"""SQLite state: contacts, cursors, operations (outbox), audit log, runs.

Personal data policy: the email address is stored only in `contacts`.
Every other table, and the logs, use `contact_key` (a salted hash).
Timestamps are UTC ISO strings with a fixed width, so they sort as text.
"""
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    contact_key    TEXT PRIMARY KEY,
    email          TEXT,                 -- only place the address lives, NULLed on erasure
    consent_ref    TEXT,                 -- the contact id in the consent platform
    consent_status TEXT, consent_at TEXT,
    email_status   TEXT, email_at   TEXT,
    erased         INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cursors (
    system TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    op_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    op_key          TEXT NOT NULL UNIQUE,   -- idempotency key
    contact_key     TEXT NOT NULL,
    target          TEXT NOT NULL,          -- "consent" or "email"
    kind            TEXT NOT NULL,          -- set_status, create, erase, ...
    desired_status  TEXT,
    effective_at    TEXT,
    source          TEXT NOT NULL,
    rule            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending, confirmed, dead
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT,
    run_id          TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ops_due ON operations(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS erasure_requests (
    request_id   TEXT PRIMARY KEY,
    contact_key  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',      -- pending, completed
    requested_at TEXT NOT NULL,
    deadline_at  TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS review_items (
    contact_key   TEXT PRIMARY KEY,
    reason        TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    run_id      TEXT,
    contact_key TEXT,
    event       TEXT NOT NULL,
    rule        TEXT,
    detail      TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    stats       TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def from_iso(value: str) -> datetime:
    return datetime.strptime(value, FMT).replace(tzinfo=timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def contact_key(email: str, salt: str) -> str:
    """Stable pseudonymous id for a contact. Same email always gives same key."""
    data = f"{salt}|{normalize_email(email)}".encode()
    return hashlib.sha256(data).hexdigest()[:16]


def connect(path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_cursor(conn, system: str) -> str | None:
    row = conn.execute("SELECT value FROM cursors WHERE system=?", (system,)).fetchone()
    return row["value"] if row else None


def set_cursor(conn, system: str, value: str) -> None:
    conn.execute(
        "INSERT INTO cursors (system, value) VALUES (?, ?) "
        "ON CONFLICT(system) DO UPDATE SET value=excluded.value", (system, value))


def enqueue_operation(conn, *, contact_key, target, kind, status, effective_at,
                      source, rule, run_id) -> bool:
    """Add an operation to the outbox. Returns False if it already exists.

    Does not commit: the caller wraps it in `with conn:` together with the
    cursor update, so both succeed or neither does.
    """
    now = to_iso(utcnow())
    at = to_iso(effective_at)
    op_key = hashlib.sha256(
        f"{contact_key}|{target}|{kind}|{status}|{at}".encode()).hexdigest()
    cur = conn.execute(
        "INSERT OR IGNORE INTO operations (op_key, contact_key, target, kind, "
        "desired_status, effective_at, source, rule, next_attempt_at, run_id, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (op_key, contact_key, target, kind, status, at, source, rule,
         now, run_id, now, now))
    return cur.rowcount == 1


def audit(conn, run_id, contact_key, event, rule=None, detail=None) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, run_id, contact_key, event, rule, detail) "
        "VALUES (?,?,?,?,?,?)",
        (to_iso(utcnow()), run_id, contact_key, event, rule, detail))
