"""Executor: works through the due operations of the outbox.

Each operation is processed independently, so one failure never blocks the
batch. Retries are scheduled in the database (next_attempt_at), not slept.
After every write the executor reads the state back and compares it with the
desired state: a write whose response was lost is therefore detected, and a
repeated "set state" call is harmless.
Logs and audit rows carry contact_key and ids, never the email address.
"""
import json
import logging
import random
from datetime import timedelta

from glue.clients import PermanentError, TransientError, consent_state, email_state
from glue.db import audit, from_iso, to_iso, utcnow
from glue.retry import MAX_ATTEMPTS, backoff_delay

log = logging.getLogger("glue.executor")


class VerificationFailed(TransientError):
    pass


def _log(level, **fields) -> None:
    log.log(level, json.dumps(fields, sort_keys=True))


def _apply(op, contact, clients) -> None:
    target, kind = op["target"], op["kind"]
    if kind not in ("set_status", "create") or (target == "consent" and kind == "create"):
        raise PermanentError(f"unsupported operation {target}/{kind}")
    if contact is None:
        raise PermanentError("contact not found in local state")
    at = from_iso(op["effective_at"])
    desired = op["desired_status"]

    if target == "email":
        if not contact["email"]:
            raise PermanentError("contact has no email (erased?)")
        client = clients["email"]
        client.set_status(contact["email"], desired, at, op["source"])
        state = client.get(contact["email"])
        ok = state is not None and email_state(state).status == desired
    elif target == "consent":
        if not contact["consent_ref"]:
            raise PermanentError("contact has no consent platform id")
        client = clients["consent"]
        client.set_status(contact["consent_ref"], desired, at, op["source"])
        state = client.get(contact["consent_ref"])
        ok = state is not None and consent_state(state).status == desired
    else:
        raise PermanentError(f"unknown target {target}")

    if not ok:
        raise VerificationFailed("read-back does not match the desired state")


def _finish(conn, op, run_id, *, status, error, next_at, event, now) -> int:
    attempts = op["attempts"] + 1
    with conn:
        conn.execute(
            "UPDATE operations SET status=?, attempts=?, last_error=?, "
            "next_attempt_at=?, updated_at=? WHERE op_id=?",
            (status, attempts, error, next_at, to_iso(now), op["op_id"]))
        detail = f"op={op['op_id']} target={op['target']} attempt={attempts}"
        if error:
            detail += f" error={error}"
        audit(conn, run_id, op["contact_key"], event, op["rule"], detail)
    return attempts


def process_due(conn, clients, run_id, *, now=None, max_attempts=MAX_ATTEMPTS,
                rng=random) -> dict:
    now = now or utcnow()
    due = conn.execute(
        "SELECT * FROM operations WHERE status='pending' AND next_attempt_at <= ? "
        "ORDER BY op_id", (to_iso(now),)).fetchall()
    stats = {"due": len(due), "confirmed": 0, "retry": 0, "dead": 0}

    for op in due:
        contact = conn.execute(
            "SELECT email, consent_ref FROM contacts WHERE contact_key=?",
            (op["contact_key"],)).fetchone()
        base = dict(run_id=run_id, op_id=op["op_id"], contact_key=op["contact_key"],
                    target=op["target"], rule=op["rule"])
        try:
            _apply(op, contact, clients)
        except TransientError as exc:
            attempts = op["attempts"] + 1
            if attempts >= max_attempts:
                _finish(conn, op, run_id, status="dead", error=str(exc),
                        next_at=op["next_attempt_at"], event="dead_lettered", now=now)
                stats["dead"] += 1
                _log(logging.ERROR, outcome="dead_lettered", attempts=attempts,
                     error=str(exc), **base)
            else:
                delay = backoff_delay(attempts, exc.retry_after, rng=rng)
                _finish(conn, op, run_id, status="pending", error=str(exc),
                        next_at=to_iso(now + timedelta(seconds=delay)),
                        event="retry_scheduled", now=now)
                stats["retry"] += 1
                _log(logging.WARNING, outcome="retry_scheduled", attempts=attempts,
                     delay_s=round(delay, 1), error=str(exc), **base)
        except PermanentError as exc:
            attempts = _finish(conn, op, run_id, status="dead", error=str(exc),
                               next_at=op["next_attempt_at"], event="dead_lettered", now=now)
            stats["dead"] += 1
            _log(logging.ERROR, outcome="dead_lettered", attempts=attempts,
                 error=str(exc), **base)
        except Exception as exc:  # a bug must not take the whole batch down
            log.exception("unexpected error in operation %s", op["op_id"])
            attempts = _finish(conn, op, run_id, status="dead",
                               error=f"unexpected {type(exc).__name__}",
                               next_at=op["next_attempt_at"], event="dead_lettered", now=now)
            stats["dead"] += 1
            _log(logging.ERROR, outcome="dead_lettered", attempts=attempts,
                 error=f"unexpected {type(exc).__name__}", **base)
        else:
            attempts = _finish(conn, op, run_id, status="confirmed", error=None,
                               next_at=op["next_attempt_at"], event="confirmed", now=now)
            stats["confirmed"] += 1
            _log(logging.INFO, outcome="confirmed", attempts=attempts, **base)
    return stats


def replay(conn, op_id: int | None = None) -> int:
    """Put dead-lettered operations back in the queue (after fixing the cause)."""
    sql = "SELECT op_id, contact_key FROM operations WHERE status='dead'"
    params: tuple = ()
    if op_id is not None:
        sql += " AND op_id=?"
        params = (op_id,)
    rows = conn.execute(sql, params).fetchall()
    now = to_iso(utcnow())
    with conn:
        for row in rows:
            conn.execute(
                "UPDATE operations SET status='pending', attempts=0, next_attempt_at=?, "
                "updated_at=? WHERE op_id=?", (now, now, row["op_id"]))
            audit(conn, None, row["contact_key"], "replayed", detail=f"op={row['op_id']}")
    return len(rows)
