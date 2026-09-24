import hashlib
from datetime import UTC, datetime

import pytest
from test_broker_grant_schema import grant
from test_broker_task_binding import seed

from k3_support.broker_grants import revoke, verify


def test_grant_verifies_peer_and_is_invalid_after_revocation(conn):
    seed(conn)
    grant(conn, token_digest=hashlib.sha256(b"synthetic").hexdigest())
    now = datetime(2026, 9, 8, 0, 30, tzinfo=UTC)
    before = list(conn.iterdump())
    result = verify(conn, token="synthetic", peer_uid=1234, now=now)
    assert result["job_id"] == "job-1" and "token_digest" not in result
    assert list(conn.iterdump()) == before
    for token, uid in (("synthetic", 1235), ("wrong", 1234)):
        with pytest.raises(ValueError):
            verify(conn, token=token, peer_uid=uid, now=now)
    revoke(conn, grant_id="grant-1", now=now)
    with pytest.raises(ValueError):
        verify(conn, token="synthetic", peer_uid=1234, now=now)


@pytest.mark.parametrize("stamp", ["2026-09-07T23:59:59+00:00", "2026-09-08T01:00:00+00:00"])
def test_grant_excludes_before_creation_and_expiry_boundary(conn, stamp):
    seed(conn)
    grant(conn, token_digest=hashlib.sha256(b"synthetic").hexdigest())
    with pytest.raises(ValueError, match="validity"):
        verify(conn, token="synthetic", peer_uid=1234, now=datetime.fromisoformat(stamp))


def test_revocation_preserves_first_time_and_does_not_cancel_job(conn):
    seed(conn)
    grant(conn)
    jobs = [tuple(row) for row in conn.execute("SELECT * FROM jobs")]
    first = revoke(conn, grant_id="grant-1", now=datetime(2026, 9, 8, tzinfo=UTC))
    later = revoke(conn, grant_id="grant-1", now=datetime(2026, 9, 9, tzinfo=UTC))
    assert first == later
    assert first["process_exit_verified"] is False
    assert first["board_cleanup_verified"] is False
    assert "token_digest" not in first
    assert [tuple(row) for row in conn.execute("SELECT * FROM jobs")] == jobs
    assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 1


@pytest.mark.parametrize("grant_id", ["missing", "", None, [], "x" * 257])
def test_bad_revocation_does_not_create_records(conn, grant_id):
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        revoke(conn, grant_id=grant_id)
    assert list(conn.iterdump()) == before
