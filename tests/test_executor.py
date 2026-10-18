from datetime import datetime, timedelta, timezone

import pytest

from glue.clients import PermanentError, TransientError
from glue.db import connect, enqueue_operation, to_iso, utcnow
from glue.executor import process_due, replay

WIRE_TS = "2026-10-18T10:05:00.000000Z"


class FakeEmail:
    """Scriptable stand-in. script steps: raise, raise_after_apply, ignore."""
    def __init__(self):
        self.store, self.script, self.calls = {}, [], 0

    def set_status(self, email, status, at, source):
        self.calls += 1
        step = self.script.pop(0) if self.script else None
        if step and step[0] == "raise":
            raise step[1]
        if not (step and step[0] == "ignore"):
            self.store[email] = status
        if step and step[0] == "raise_after_apply":
            raise step[1]

    def get(self, email):
        status = self.store.get(email)
        if status is None:
            return None
        wire = "subscribed" if status == "opted_in" else "unsubscribed"
        return {"status": wire, "status_changed_at": WIRE_TS}


class FakeConsent:
    def __init__(self):
        self.store, self.script = {}, []

    def set_status(self, contact_id, status, at, source):
        step = self.script.pop(0) if self.script else None
        if step and step[0] == "raise":
            raise step[1]
        self.store[contact_id] = status

    def get(self, contact_id):
        status = self.store.get(contact_id)
        if status is None:
            return None
        return {"consent_status": status, "consent_effective_at": WIRE_TS}


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def add_contact(conn, key="k1", email="a@example.com", ref="1"):
    with conn:
        conn.execute("INSERT INTO contacts (contact_key, email, consent_ref, updated_at) "
                     "VALUES (?,?,?,?)", (key, email, ref, to_iso(utcnow())))


def add_op(conn, key="k1", target="email", status="withdrawn", minute=5):
    with conn:
        enqueue_operation(conn, contact_key=key, target=target, kind="set_status",
                          status=status,
                          effective_at=datetime(2026, 10, 18, 10, minute, tzinfo=timezone.utc),
                          source="sync_from_consent", rule="R2_propagate_withdrawal",
                          run_id="run0")


def op(conn, op_id=1):
    return conn.execute("SELECT * FROM operations WHERE op_id=?", (op_id,)).fetchone()


def clients(email=None, consent=None):
    return {"email": email or FakeEmail(), "consent": consent or FakeConsent()}


def test_success_confirms_and_writes_audit(conn):
    add_contact(conn); add_op(conn)
    email = FakeEmail()
    stats = process_due(conn, clients(email), "r1")
    assert stats["confirmed"] == 1
    assert email.store["a@example.com"] == "withdrawn"
    row = op(conn)
    assert row["status"] == "confirmed" and row["attempts"] == 1
    events = [r["event"] for r in conn.execute("SELECT event FROM audit_log")]
    assert events == ["confirmed"]


def test_transient_failure_is_rescheduled_and_respects_retry_after(conn):
    add_contact(conn); add_op(conn)
    email = FakeEmail()
    email.script = [("raise", TransientError("rate limited", retry_after=30))]
    now = utcnow()
    assert process_due(conn, clients(email), "r1", now=now)["retry"] == 1
    row = op(conn)
    assert row["status"] == "pending" and row["attempts"] == 1
    assert row["next_attempt_at"] >= to_iso(now + timedelta(seconds=30))
    assert process_due(conn, clients(email), "r2", now=now + timedelta(seconds=10))["due"] == 0
    assert process_due(conn, clients(email), "r3", now=now + timedelta(seconds=31))["confirmed"] == 1


def test_permanent_error_goes_straight_to_dead_letter(conn):
    add_contact(conn); add_op(conn)
    email = FakeEmail()
    email.script = [("raise", PermanentError("email: HTTP 422 bad payload"))]
    assert process_due(conn, clients(email), "r1")["dead"] == 1
    row = op(conn)
    assert row["status"] == "dead" and row["attempts"] == 1 and "422" in row["last_error"]
    assert process_due(conn, clients(email), "r2", now=utcnow() + timedelta(hours=1))["due"] == 0


def test_dead_letter_after_max_attempts(conn):
    add_contact(conn); add_op(conn)
    email = FakeEmail()
    email.script = [("raise", TransientError("down"))] * 3
    base = utcnow()
    for i in range(3):
        process_due(conn, clients(email), f"r{i}", now=base + timedelta(hours=i + 1), max_attempts=3)
    row = op(conn)
    assert row["status"] == "dead" and row["attempts"] == 3


def test_one_failure_does_not_block_the_batch(conn):
    add_contact(conn, "k1", "a@example.com"); add_contact(conn, "k2", "b@example.com")
    add_op(conn, "k1"); add_op(conn, "k2")
    email = FakeEmail()
    email.script = [("raise", PermanentError("email: HTTP 422"))]   # hits the first call only
    stats = process_due(conn, clients(email), "r1")
    assert stats["dead"] == 1 and stats["confirmed"] == 1
    assert email.store == {"b@example.com": "withdrawn"}


def test_lost_response_is_safe_to_retry(conn):
    """The write is applied but the client sees a timeout (the 'bob' case)."""
    add_contact(conn); add_op(conn)
    email = FakeEmail()
    email.script = [("raise_after_apply", TransientError("email: timeout"))]
    now = utcnow()
    process_due(conn, clients(email), "r1", now=now)
    assert op(conn)["status"] == "pending"
    assert email.store["a@example.com"] == "withdrawn"        # it was applied already
    process_due(conn, clients(email), "r2", now=now + timedelta(minutes=10))
    assert op(conn)["status"] == "confirmed"
    assert email.store == {"a@example.com": "withdrawn"}      # repeating changed nothing


def test_read_back_mismatch_is_retried(conn):
    add_contact(conn); add_op(conn)
    email = FakeEmail()
    email.store["a@example.com"] = "opted_in"
    email.script = [("ignore",)]                              # write accepted but not applied
    now = utcnow()
    process_due(conn, clients(email), "r1", now=now)
    row = op(conn)
    assert row["status"] == "pending" and "read-back" in row["last_error"]
    process_due(conn, clients(email), "r2", now=now + timedelta(minutes=10))
    assert op(conn)["status"] == "confirmed"


def test_replay_requeues_dead_operations(conn):
    add_contact(conn); add_op(conn)
    email = FakeEmail()
    email.script = [("raise", PermanentError("email: HTTP 422"))]
    process_due(conn, clients(email), "r1")
    assert op(conn)["status"] == "dead"
    assert replay(conn) == 1
    row = op(conn)
    assert row["status"] == "pending" and row["attempts"] == 0
    assert process_due(conn, clients(email), "r2")["confirmed"] == 1


def test_consent_target_uses_the_consent_platform_id(conn):
    add_contact(conn, ref="42"); add_op(conn, target="consent")
    consent = FakeConsent()
    assert process_due(conn, clients(consent=consent), "r1")["confirmed"] == 1
    assert consent.store == {"42": "withdrawn"}


def test_missing_contact_data_is_permanent(conn):
    add_contact(conn, email=None); add_op(conn)
    assert process_due(conn, clients(), "r1")["dead"] == 1
    assert "no email" in op(conn)["last_error"]
