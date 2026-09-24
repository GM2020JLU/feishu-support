import sqlite3

import pytest
from test_broker_task_binding import seed


def grant(conn, **changes):
    data = {"grant_id": "grant-1", "token_digest": "b" * 64, "worker_uid": 1234,
            "job_id": "job-1", "attempt_no": 1, "lifecycle_round": 1,
            "input_digest": "a" * 64, "lease_owner": "worker",
            "created_at": "2026-09-08T00:00:00+00:00", "expires_at": "2026-09-08T01:00:00+00:00", "revoked_at": None}
    data.update(changes)
    conn.execute("INSERT INTO broker_grants VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(data.values()))


def test_grant_schema_binds_existing_job_and_unique_attempt(conn):
    seed(conn)
    grant(conn)
    with pytest.raises(sqlite3.IntegrityError):
        grant(conn, grant_id="grant-2", token_digest="c" * 64)
    grant(conn, grant_id="grant-2", token_digest="c" * 64, attempt_no=2)
    assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 2
    assert list(conn.execute("PRAGMA foreign_key_check")) == []


@pytest.mark.parametrize("changes", [{"worker_uid": 0}, {"attempt_no": 0}, {"lifecycle_round": 0},
                                    {"token_digest": "G" * 64}, {"token_digest": "short"},
                                    {"lease_owner": ""}, {"job_id": "missing"}])
def test_invalid_grant_cannot_be_persisted(conn, changes):
    seed(conn)
    with pytest.raises(sqlite3.IntegrityError):
        grant(conn, **changes)
    assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 0
