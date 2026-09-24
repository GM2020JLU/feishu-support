import hashlib
from datetime import UTC, datetime

import pytest
from test_broker_grant_schema import grant
from test_broker_task_binding import seed

from k3_support.broker_grants import verify_bound_task
from k3_support.db import transaction


def setup(conn):
    params = seed(conn)
    grant(conn, token_digest=hashlib.sha256(b"broker-secret").hexdigest())
    params["lease_token"] = "broker-secret"
    return params


def test_bound_grant_requires_transaction_and_uses_separate_secret(conn):
    params = setup(conn)
    now = datetime(2026, 9, 8, 0, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match="transaction"):
        verify_bound_task(conn, params, peer_uid=1234, now=now)
    before = list(conn.iterdump())
    with transaction(conn):
        assert verify_bound_task(conn, params, peer_uid=1234, now=now)["job_id"] == "job-1"
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("change", ["attempt_no=2", "lease_owner='replacement'", "state='cancelled'", "lifecycle_round=2", "input_digest='changed'", "lease_expires_at=NULL"])
def test_grant_does_not_override_changed_job(conn, change):
    params = setup(conn)
    conn.execute(f"UPDATE jobs SET {change} WHERE job_id='job-1'")
    with transaction(conn), pytest.raises(ValueError):
        verify_bound_task(conn, params, peer_uid=1234, now=datetime(2026, 9, 8, 0, 30, tzinfo=UTC))
