import os
from datetime import UTC, datetime

from test_attention_races import race
from test_broker_task_binding import seed

from k3_support.broker_grants import issue, revoke, verify
from k3_support.db import transaction


def test_concurrent_issue_has_exactly_one_secret(conn, config):
    seed(conn)
    now = datetime(2026, 9, 8, tzinfo=UTC)

    def mint(other):
        try:
            with transaction(other):
                return issue(other, job_id="job-1", attempt_no=1, lease_owner="worker",
                             worker_uid=os.geteuid()+1, now=now)
        except ValueError as error:
            return str(error)

    results = race(config, mint, mint)
    successes = [result for result in results if isinstance(result, dict)]
    assert len(successes) == 1
    assert "attempt already has a grant" in results
    assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 1
    assert verify(conn, token=successes[0]["token"], peer_uid=os.geteuid()+1, now=now)["job_id"] == "job-1"


def test_concurrent_revoke_preserves_single_first_receipt(conn, config):
    seed(conn)
    now = datetime(2026, 9, 8, tzinfo=UTC)
    with transaction(conn):
        granted = issue(conn, job_id="job-1", attempt_no=1, lease_owner="worker", worker_uid=os.geteuid()+1, now=now)
    left, right = race(config,
                       lambda other: revoke(other, grant_id=granted["grant_id"], now=now),
                       lambda other: revoke(other, grant_id=granted["grant_id"], now=datetime(2026, 9, 9, tzinfo=UTC)))
    assert left == right
    assert left["revoked_at"] in (now.isoformat(), datetime(2026, 9, 9, tzinfo=UTC).isoformat())
    assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 1
