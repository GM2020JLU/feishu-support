import sqlite3

import pytest
from test_broker_grant_schema import grant
from test_broker_task_binding import seed


def insert(conn, **changes):
    value = {"peer_uid": 1234, "request_id": "request-1", "request_digest": "a" * 64,
             "method": "renew", "grant_id": "grant-1", "response_json": '{"accepted":true}',
             "created_at": "2026-09-08T00:00:00+00:00"}
    value.update(changes)
    conn.execute("INSERT INTO broker_receipts VALUES(?,?,?,?,?,?,?)", tuple(value.values()))


def test_receipt_uniqueness_is_scoped_to_peer(conn):
    seed(conn)
    grant(conn)
    insert(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn)
    insert(conn, peer_uid=1235)
    assert conn.execute("SELECT count(*) FROM broker_receipts").fetchone()[0] == 2


@pytest.mark.parametrize("change", [{"method": "claim"}, {"method": "approve"}, {"grant_id": "missing"},
                                    {"response_json": "broken"}, {"response_json": '"' + "x" * 262144 + '"'},
                                    {"peer_uid": 0}, {"request_digest": "short"}])
def test_receipt_schema_rejects_invalid_records(conn, change):
    seed(conn)
    grant(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, **change)
    assert conn.execute("SELECT count(*) FROM broker_receipts").fetchone()[0] == 0
