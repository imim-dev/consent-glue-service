"""Command line interface: run-once, status, dead-letters, replay."""
import argparse
import json
import logging
import os
import sys

from glue.clients import ConsentClient, EmailClient
from glue.db import connect, from_iso, utcnow
from glue.engine import run_once
from glue.executor import replay


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="glue", description="Consent glue service")
    p.add_argument("--db", default=os.getenv("GLUE_DB", "data/glue.db"))
    p.add_argument("--consent-url", default=os.getenv("GLUE_CONSENT_URL", "http://127.0.0.1:8001"))
    p.add_argument("--email-url", default=os.getenv("GLUE_EMAIL_URL", "http://127.0.0.1:8002"))
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run-once", help="fetch changes, decide, execute")
    r.add_argument("--dry-run", action="store_true", help="show decisions, change nothing")
    r.add_argument("--force", action="store_true", help="bypass the mass-change guard")
    sub.add_parser("status", help="operations, oldest pending, dead letters, last run")
    sub.add_parser("dead-letters", help="list failed operations with reasons")
    rp = sub.add_parser("replay", help="requeue dead-lettered operations")
    rp.add_argument("--op-id", type=int, help="only this operation (default: all)")
    return p


def cmd_run(args, conn) -> int:
    clients = {"consent": ConsentClient(args.consent_url),
               "email": EmailClient(args.email_url)}
    try:
        stats = run_once(conn, clients, salt=os.getenv("GLUE_SALT", "dev-only-salt"),
                         dry_run=args.dry_run, force=args.force)
    finally:
        for client in clients.values():
            client.close()
    print(json.dumps(stats, indent=2))
    return 2 if stats["status"] == "halted" else 0


def cmd_status(conn) -> int:
    now = utcnow()
    ops = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM operations GROUP BY status")}
    print("operations:", ", ".join(f"{k}={v}" for k, v in sorted(ops.items())) or "none")
    oldest = conn.execute(
        "SELECT MIN(created_at) FROM operations WHERE status='pending'").fetchone()[0]
    if oldest:
        print(f"oldest pending: {int((now - from_iso(oldest)).total_seconds())}s")
    dead = ops.get("dead", 0)
    print(f"dead letters: {dead}" + ("  (see: dead-letters)" if dead else ""))
    review = conn.execute("SELECT COUNT(*) FROM review_items").fetchone()[0]
    print(f"contacts needing review: {review}")
    pending = conn.execute(
        "SELECT COUNT(*), MIN(deadline_at) FROM erasure_requests WHERE status='pending'").fetchone()
    print(f"pending erasure requests: {pending[0]}"
          + (f", nearest deadline {pending[1]}" if pending[0] else ""))
    last = conn.execute(
        "SELECT run_id, started_at, status FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    print("last run:", f"{last['run_id']} at {last['started_at']} ({last['status']})"
          if last else "none")
    return 0


def cmd_dead_letters(conn) -> int:
    rows = conn.execute(
        "SELECT op_id, target, rule, attempts, last_error, contact_key "
        "FROM operations WHERE status='dead' ORDER BY op_id").fetchall()
    if not rows:
        print("no dead-lettered operations")
    for r in rows:
        print(f"op {r['op_id']}: target={r['target']} rule={r['rule']} "
              f"attempts={r['attempts']} contact={r['contact_key']} error={r['last_error']}")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request URLs, and the email address is part of the URL.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    conn = connect(args.db)
    try:
        if args.command == "run-once":
            return cmd_run(args, conn)
        if args.command == "status":
            return cmd_status(conn)
        if args.command == "dead-letters":
            return cmd_dead_letters(conn)
        count = replay(conn, args.op_id)
        print(f"requeued {count} operation(s)")
        return 0
    finally:
        conn.close()
