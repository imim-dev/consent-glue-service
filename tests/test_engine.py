from datetime import datetime, timedelta, timezone

import pytest

from glue.clients import TransientError
from glue.db import connect, get_cursor, to_iso
from glue.engine import run_once

BASE = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
SALT = "test-salt"
WIRE = {"opted_in": "subscribed", "withdrawn": "unsubscribed"}


class FakeConsent:
    def __init__(self):
        self.rows, self.tick, self.fail = {}, 0, None

    def _ts(self):
        self.tick += 1
        return to_iso(BASE + timedelta(seconds=self.tick))

    def add(self, email, status, at=BASE):
        cid = len(self.rows) + 1
        self.rows[cid] = {"id": cid, "email": email, "consent_status": status,
                          "consent_effective_at": to_iso(at), "consent_source": "seed",
                          "updated_at": self._ts()}

    def fetch_changes(self, since):
        if self.fail:
            raise self.fail
        rows = sorted(self.rows.values(), key=lambda r: r["updated_at"])
        return [r for r in rows if not since or r["updated_at"] >= since]

    def set_status(self, cid, status, at, source):
        self.rows[int(cid)].update(consent_status=status,
                                   consent_effective_at=to_iso(at),
                                   updated_at=self._ts())

    def get(self, cid):
        return self.rows.get(int(cid))


class FakeEmail:
    def __init__(self):
        self.rows, self.tick, self.fail = {}, 0, None

    def _ts(self):
        self.tick += 1
        return to_iso(BASE + timedelta(seconds=self.tick))

    def add(self, email, status, at=BASE):
        self.set_status(email, status, at, "seed")

    def fetch_changes(self, since):
        if self.fail:
            raise self.fail
        rows = sorted(self.rows.values(), key=lambda r: r["updated_at"])
        return [r for r in rows if not since or r["updated_at"] >= since]

    def set_status(self, email, status, at, source):
        self.rows[email] = {"email": email, "status": WIRE[status],
                            "status_changed_at": to_iso(at), "source": source,
                            "updated_at": self._ts()}

    def get(self, email):
        return self.rows.get(email)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def world():
    """Same shape as the simulators' seed data, plus bob unsubscribing in email."""
    c, e = FakeConsent(), FakeEmail()
    c.add("alice@example.com", "opted_in")
    c.add("bob@example.com", "opted_in")
    c.add("carol@example.com", "withdrawn")
    c.add("erin@example.com", "opted_in")
    e.add("alice@example.com", "opted_in")
    e.add("bob@example.com", "withdrawn", at=BASE + HOUR)   # clicked unsubscribe
    e.add("carol@example.com", "opted_in")                  # conflict
    e.add("frank@example.com", "opted_in")                  # unknown to consent platform
    return c, e


def run(conn, c, e, **kw):
    return run_once(conn, {"consent": c, "email": e}, salt=SALT, **kw)


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_first_sync_plans_the_expected_operations(conn):
    c, e = world()
    r = run(conn, c, e, execute=False)
    assert r["status"] == "ok" and r["fetched"] == {"consent": 4, "email": 4}
    planned = {tuple(row) for row in conn.execute("SELECT target, rule FROM operations")}
    assert planned == {("consent", "R3_withdrawal_from_follower"),
                       ("email", "R2_propagate_withdrawal"),
                       ("email", "R5_create_missing")}
    assert r["new_ops"] == 3 and r["review"] == 1
    assert conn.execute("SELECT reason FROM review_items").fetchone()[0] == "unknown_contact"


def test_end_to_end_converges_without_echo(conn):
    c, e = world()
    r1 = run(conn, c, e)
    assert r1["executor"]["confirmed"] == 3
    assert e.rows["carol@example.com"]["status"] == "unsubscribed"
    assert e.rows["erin@example.com"]["status"] == "subscribed"
    assert c.rows[2]["consent_status"] == "withdrawn"       # bob, recorded from email
    for _ in range(2):                                      # our own writes come back
        r = run(conn, c, e)
        assert r["new_ops"] == 0 and r["executor"]["due"] == 0


def test_rerun_over_the_same_data_creates_no_duplicates(conn):
    c, e = world()
    run(conn, c, e, execute=False)
    conn.execute("DELETE FROM cursors")
    conn.commit()                                           # force a full re-read
    r = run(conn, c, e, execute=False)
    assert r["new_ops"] == 0 and count(conn, "operations") == 3


def test_failing_system_skips_planning_on_the_first_sync(conn):
    c, e = world()
    e.fail = TransientError("email down")
    r = run(conn, c, e, execute=False)
    assert r["status"] == "partial" and "email" in r["fetch_errors"]
    assert r["planning_skipped"] and r["new_ops"] == 0 and count(conn, "operations") == 0
    assert get_cursor(conn, "consent") is not None
    assert get_cursor(conn, "email") is None                # will be re-read next time
    assert count(conn, "contacts") == 4


def test_failing_system_after_first_sync_still_plans_from_known_state(conn):
    c, e = world()
    run(conn, c, e, execute=False)
    c.set_status(1, "withdrawn", BASE + 2 * HOUR, "preference_center")   # alice
    e.fail = TransientError("email down")
    r = run(conn, c, e, execute=False)
    assert r["status"] == "partial" and r["new_ops"] == 1
    assert count(conn, "operations") == 4


def test_mass_change_guard_halts_and_rolls_back(conn):
    c, e = world()
    r = run(conn, c, e, execute=False, max_change_ratio=0.3, min_change_floor=1)
    assert r["status"] == "halted" and r["halt_reason"]
    assert (count(conn, "operations"), count(conn, "cursors"), count(conn, "contacts")) == (0, 0, 0)
    r = run(conn, c, e, execute=False, max_change_ratio=0.3, min_change_floor=1, force=True)
    assert r["status"] == "ok" and r["new_ops"] == 3


def test_dry_run_changes_nothing(conn):
    c, e = world()
    r = run(conn, c, e, dry_run=True)
    assert r["status"] == "dry_run" and r["new_ops"] == 3
    for table in ("operations", "contacts", "cursors", "runs", "review_items"):
        assert count(conn, table) == 0, table


def test_new_decision_supersedes_a_stale_pending_operation(conn):
    c, e = FakeConsent(), FakeEmail()
    c.add("alice@example.com", "opted_in")
    e.add("alice@example.com", "opted_in")
    assert run(conn, c, e, execute=False)["new_ops"] == 0
    c.set_status(1, "withdrawn", BASE + HOUR, "preference_center")
    assert run(conn, c, e, execute=False)["new_ops"] == 1
    c.set_status(1, "opted_in", BASE + 2 * HOUR, "preference_center")    # re-consent
    r = run(conn, c, e, execute=False)
    assert r["superseded"] == 1 and r["new_ops"] == 0
    assert [row[0] for row in conn.execute("SELECT status FROM operations")] == ["superseded"]
    r = run(conn, c, e)                                     # now execute
    assert r["executor"]["due"] == 0
    assert e.rows["alice@example.com"]["status"] == "subscribed"   # not suppressed by a stale op


def test_drift_back_to_a_fixed_state_creates_a_new_operation(conn):
    """Carol withdrew. The glue fixed the email platform. Later an import re-subscribes
    her: the same fix must run again, it must not be ignored as a duplicate."""
    c, e = FakeConsent(), FakeEmail()
    c.add("carol@example.com", "withdrawn")
    e.add("carol@example.com", "opted_in")
    r = run(conn, c, e)
    assert r["new_ops"] == 1 and r["executor"]["confirmed"] == 1
    assert e.rows["carol@example.com"]["status"] == "unsubscribed"
    e.set_status("carol@example.com", "opted_in", BASE + 3 * HOUR, "import")
    r = run(conn, c, e)
    assert r["new_ops"] == 1 and r["executor"]["confirmed"] == 1
    assert e.rows["carol@example.com"]["status"] == "unsubscribed"


def test_no_email_address_outside_the_contacts_table(conn):
    c, e = world()
    run(conn, c, e)
    for table in ("operations", "audit_log", "runs", "review_items",
                  "cursors", "erasure_requests"):
        dump = str([tuple(r) for r in conn.execute(f"SELECT * FROM {table}")])
        assert "@" not in dump, table
