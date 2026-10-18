"""Sync engine: ingest changes, decide, write the outbox, then execute.

One run:
  1. Fetch changes from both systems since their cursors (network, no DB lock).
  2. In ONE transaction: update the local snapshot, run the pure policy for each
     touched contact, write operations to the outbox, advance the cursors.
     A crash cannot lose a detected change, and a re-run cannot duplicate it.
  3. Execute the due operations (retries, dead-letter, read-back).
Safety nets: mass-change guard, dry-run, failure isolation per system.
Logs and audit rows carry contact_key, never the email address.
"""
import json
import logging
import uuid

from glue.clients import PermanentError, TransientError, consent_state, email_state
from glue.db import (audit, contact_key, enqueue_operation, from_iso, get_cursor,
                     make_op_key, normalize_email, set_cursor, to_iso, utcnow)
from glue.executor import process_due
from glue.policy import SourceState, decide
from glue.retry import MAX_ATTEMPTS

log = logging.getLogger("glue.engine")
SYSTEMS = ("consent", "email")


class Halted(Exception):
    """The run planned suspiciously many changes: stop and ask a human."""


class _DryRun(Exception):
    pass


def _log(level, **fields) -> None:
    log.log(level, json.dumps(fields, sort_keys=True))


def _fetch(conn, clients, stats) -> dict:
    """A failing system is skipped for this run, the other one still syncs."""
    fetched = {}
    for system in SYSTEMS:
        since = get_cursor(conn, system) or None
        try:
            fetched[system] = clients[system].fetch_changes(since)
        except (TransientError, PermanentError) as exc:
            stats["fetch_errors"][system] = str(exc)
            _log(logging.WARNING, outcome="fetch_failed", system=system, error=str(exc))
    return fetched


def _upsert(conn, system, item, salt) -> str:
    """Store the system's current view of a contact. Returns the contact_key."""
    key = contact_key(item["email"], salt)
    email = normalize_email(item["email"])
    now = to_iso(utcnow())
    if system == "consent":
        st = consent_state(item)
        conn.execute(
            "INSERT INTO contacts (contact_key, email, consent_ref, consent_status, "
            "consent_at, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(contact_key) DO UPDATE SET email=excluded.email, "
            "consent_ref=excluded.consent_ref, consent_status=excluded.consent_status, "
            "consent_at=excluded.consent_at, updated_at=excluded.updated_at "
            "WHERE contacts.erased=0",
            (key, email, str(item["id"]), st.status, to_iso(st.at), now))
    else:
        st = email_state(item)
        conn.execute(
            "INSERT INTO contacts (contact_key, email, email_status, email_at, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(contact_key) DO UPDATE SET email=excluded.email, "
            "email_status=excluded.email_status, email_at=excluded.email_at, "
            "updated_at=excluded.updated_at WHERE contacts.erased=0",
            (key, email, st.status, to_iso(st.at), now))
    return key


def _states(row) -> dict:
    consent = (SourceState(row["consent_status"], from_iso(row["consent_at"]))
               if row["consent_status"] else None)
    email = (SourceState(row["email_status"], from_iso(row["email_at"]))
             if row["email_status"] else None)
    return {"consent": consent, "email": email}


def _observed(row, target: str) -> str:
    """The target's state at planning time, part of the idempotency key."""
    status = row[f"{target}_status"]
    return f"{status}@{row[f'{target}_at']}" if status else "none"


def _supersede_stale(conn, key, current_keys) -> int:
    """Pending operations that no longer match the current decision are obsolete."""
    sql = ("UPDATE operations SET status='superseded', updated_at=? "
           "WHERE contact_key=? AND status='pending'")
    params = [to_iso(utcnow()), key]
    if current_keys:
        sql += f" AND op_key NOT IN ({','.join('?' * len(current_keys))})"
        params += list(current_keys)
    return conn.execute(sql, params).rowcount


def _plan_contact(conn, key, run_id) -> dict:
    result = {"new_ops": 0, "superseded": 0, "review": 0}
    row = conn.execute("SELECT * FROM contacts WHERE contact_key=? AND erased=0",
                       (key,)).fetchone()
    if row is None:
        return result
    decision = decide(_states(row))

    if decision.review:
        cur = conn.execute(
            "INSERT OR IGNORE INTO review_items (contact_key, reason, first_seen_at) "
            "VALUES (?,?,?)", (key, decision.review, to_iso(utcnow())))
        if cur.rowcount:
            result["review"] = 1
            audit(conn, run_id, key, "review_flagged", detail=decision.review)
        return result
    conn.execute("DELETE FROM review_items WHERE contact_key=?", (key,))

    current = []
    for a in decision.actions:
        observed = _observed(row, a.target)
        current.append(make_op_key(key, a.target, a.kind, a.status, a.at, observed))
        if enqueue_operation(conn, contact_key=key, target=a.target, kind=a.kind,
                             status=a.status, effective_at=a.at, source=a.source,
                             rule=a.rule, run_id=run_id, observed=observed):
            result["new_ops"] += 1
            audit(conn, run_id, key, "op_planned", a.rule,
                  f"target={a.target} kind={a.kind} status={a.status}")
    result["superseded"] = _supersede_stale(conn, key, current)
    if result["superseded"]:
        audit(conn, run_id, key, "ops_superseded", detail=f"count={result['superseded']}")
    return result


def run_once(conn, clients, *, salt, run_id=None, dry_run=False, force=False,
             execute=True, max_change_ratio=0.5, min_change_floor=10,
             max_attempts=MAX_ATTEMPTS, now=None) -> dict:
    run_id = run_id or uuid.uuid4().hex[:8]
    stats = {"run_id": run_id, "status": "ok", "fetched": {}, "fetch_errors": {},
             "new_ops": 0, "superseded": 0, "review": 0, "planning_skipped": None,
             "halt_reason": None, "executor": None}
    if not dry_run:
        with conn:
            conn.execute("INSERT INTO runs (run_id, started_at, status) VALUES (?,?,?)",
                         (run_id, to_iso(utcnow()), "running"))

    fetched = _fetch(conn, clients, stats)
    try:
        with conn:   # one transaction: snapshot + outbox + cursors
            touched = set()
            for system, items in fetched.items():
                stats["fetched"][system] = len(items)
                for item in items:
                    touched.add(_upsert(conn, system, item, salt))

            # Planning needs a successful first sync of BOTH systems, otherwise a
            # contact that merely was not seen yet looks "missing" and would be created.
            ready = all(s in fetched or get_cursor(conn, s) is not None for s in SYSTEMS)
            if ready:
                for key in sorted(touched):
                    result = _plan_contact(conn, key, run_id)
                    for field in ("new_ops", "superseded", "review"):
                        stats[field] += result[field]
                total = conn.execute(
                    "SELECT COUNT(*) FROM contacts WHERE erased=0").fetchone()[0]
                limit = max(min_change_floor, max_change_ratio * total)
                if not force and stats["new_ops"] > limit:
                    raise Halted(f"{stats['new_ops']} planned changes exceed the limit "
                                 f"of {limit:.0f} ({total} contacts), use --force if intended")
            else:
                stats["planning_skipped"] = "a system has not synced successfully yet"

            for system, items in fetched.items():
                old = get_cursor(conn, system) or ""
                new = max((i["updated_at"] for i in items), default="")
                set_cursor(conn, system, max(old, new))
            if dry_run:
                raise _DryRun
    except _DryRun:
        stats["status"] = "dry_run"
    except Halted as exc:
        stats.update(status="halted", halt_reason=str(exc),
                     new_ops=0, superseded=0, review=0)
        _log(logging.ERROR, outcome="halted", run_id=run_id, reason=str(exc))

    if stats["status"] == "ok" and stats["fetch_errors"]:
        stats["status"] = "partial"
    if execute and not dry_run and stats["status"] in ("ok", "partial"):
        stats["executor"] = process_due(conn, clients, run_id, now=now,
                                        max_attempts=max_attempts)
    if not dry_run:
        with conn:
            conn.execute("UPDATE runs SET finished_at=?, status=?, stats=? WHERE run_id=?",
                         (to_iso(utcnow()), stats["status"], json.dumps(stats), run_id))
    _log(logging.INFO, outcome="run_finished", run_id=run_id, status=stats["status"],
         new_ops=stats["new_ops"], superseded=stats["superseded"], review=stats["review"])
    return stats
