from datetime import datetime, timezone

import pytest

from glue.db import connect, contact_key, enqueue_operation, get_cursor, set_cursor

AT = datetime(2026, 10, 18, 10, 5, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()  # Windows cannot delete an open database file


def enqueue(conn, **override):
    args = dict(contact_key="k1", target="email", kind="set_status",
                status="withdrawn", effective_at=AT,
                source="sync_from_consent", rule="R2_propagate_withdrawal",
                run_id="run1")
    args.update(override)
    return enqueue_operation(conn, **args)


def count_ops(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]


def test_enqueue_is_idempotent(conn):
    with conn:
        assert enqueue(conn) is True
        assert enqueue(conn) is False          # same change detected twice
    assert count_ops(conn) == 1
    with conn:
        later = datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)
        assert enqueue(conn, effective_at=later) is True   # a new change
    assert count_ops(conn) == 2


def test_cursor_and_operations_commit_together(conn):
    with pytest.raises(RuntimeError):
        with conn:
            set_cursor(conn, "consent", "2026-10-18T10:00:00.000000Z")
            enqueue(conn)
            raise RuntimeError("crash before commit")
    assert get_cursor(conn, "consent") is None     # rolled back together
    assert count_ops(conn) == 0

    with conn:
        set_cursor(conn, "consent", "2026-10-18T10:00:00.000000Z")
        enqueue(conn)
    assert get_cursor(conn, "consent") == "2026-10-18T10:00:00.000000Z"
    assert count_ops(conn) == 1


def test_contact_key_is_stable_normalized_and_hides_the_email():
    key = contact_key("  Alice@Example.com ", "salt")
    assert key == contact_key("alice@example.com", "salt")
    assert key != contact_key("alice@example.com", "other-salt")
    assert "alice" not in key


def test_schema_can_be_created_twice(tmp_path):
    connect(tmp_path / "x.db").close()
    connect(tmp_path / "x.db").close()             # no error on second start
