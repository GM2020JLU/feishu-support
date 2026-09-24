import os
from datetime import UTC, datetime

import pytest
from test_broker_task_binding import seed

from k3_support.broker_grants import issue, verify_bound_task
from k3_support.db import transaction


def test_issued_secret_is_independent_and_stored_only_as_digest(conn):
    params = seed(conn)
    args = {"job_id": "job-1", "attempt_no": 1, "lease_owner": "worker", "worker_uid": os.geteuid() + 1,
            "now": datetime(2026, 9, 8, tzinfo=UTC)}
    with pytest.raises(ValueError, match="transaction"):
        issue(conn, **args)
    with transaction(conn):
        issued = issue(conn, **args)
        assert issued["token"] != params["lease_token"] and len(issued["token"]) >= 40
        params["lease_token"] = issued["token"]
        assert verify_bound_task(conn, params, peer_uid=args["worker_uid"], now=args["now"])["grant_id"] == issued["grant_id"]
    assert issued["token"] not in "\n".join(conn.iterdump())
    assert issued["expires_at"] == "2026-09-08T00:05:00+00:00"
    with transaction(conn), pytest.raises(ValueError, match="already"):
        issue(conn, **args)


def test_issue_rolls_back_with_outer_claim_transaction(conn):
    seed(conn)
    with pytest.raises(RuntimeError), transaction(conn):
        issue(conn, job_id="job-1", attempt_no=1, lease_owner="worker", worker_uid=os.geteuid()+1,
              now=datetime(2026, 9, 8, tzinfo=UTC))
        raise RuntimeError("rollback")
    assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["state='cancelled'", "attempt_no=2", "lease_owner='another'",
                                    "lifecycle_round=2", "lease_expires_at=NULL",
                                    "lease_expires_at='2026-09-08T00:00:00+00:00'"])
def test_stale_job_cannot_receive_grant(conn, change):
    seed(conn)
    conn.execute(f"UPDATE jobs SET {change} WHERE job_id='job-1'")
    with transaction(conn), pytest.raises(ValueError):
        issue(conn, job_id="job-1", attempt_no=1, lease_owner="worker", worker_uid=os.geteuid()+1,
              now=datetime(2026, 9, 8, tzinfo=UTC))
    assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 0


@pytest.mark.parametrize("uid", [0, True, -1, "1234", None])
def test_invalid_worker_identity_cannot_receive_grant(conn, uid):
    seed(conn)
    with transaction(conn), pytest.raises(ValueError, match="UID"):
        issue(conn, job_id="job-1", attempt_no=1, lease_owner="worker", worker_uid=uid)
    assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 0


def test_same_uid_rejected_and_short_job_lease_caps_grant(conn):
    seed(conn)
    now = datetime(2026, 9, 8, tzinfo=UTC)
    with transaction(conn), pytest.raises(ValueError, match="UID"):
        issue(conn, job_id="job-1", attempt_no=1, lease_owner="worker", worker_uid=os.geteuid(), now=now)
    conn.execute("UPDATE jobs SET lease_expires_at='2026-09-08T00:01:00+00:00' WHERE job_id='job-1'")
    with transaction(conn):
        value = issue(conn, job_id="job-1", attempt_no=1, lease_owner="worker", worker_uid=os.geteuid()+1, now=now)
    assert value["expires_at"] == "2026-09-08T00:01:00+00:00"
