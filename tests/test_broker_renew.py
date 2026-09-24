from datetime import UTC, datetime

import pytest
from test_broker_receipts import setup

from k3_support.broker_renew import renew


def test_renew_capped_by_job_lease_and_replay_does_not_extend(conn):
    request = setup(conn)
    conn.execute("UPDATE jobs SET lease_expires_at='2026-09-08T00:33:00+00:00'")
    before = dict(conn.execute("SELECT * FROM jobs").fetchone())
    result = renew(conn, request, peer_uid=1234, now=datetime(2026, 9, 8, 0, 30, tzinfo=UTC))
    assert result["expires_at"] == "2026-09-08T00:33:00+00:00"
    assert dict(conn.execute("SELECT * FROM jobs").fetchone()) == before
    snapshot = list(conn.iterdump())
    assert renew(conn, request, peer_uid=1234, now=datetime(2026, 9, 8, 0, 31, tzinfo=UTC)) == result
    assert list(conn.iterdump()) == snapshot


def test_renew_has_five_minute_cap(conn):
    request = setup(conn)
    result = renew(conn, request, peer_uid=1234, now=datetime(2026, 9, 8, 0, 30, tzinfo=UTC))
    assert result["expires_at"] == "2026-09-08T00:35:00+00:00"


@pytest.mark.parametrize("change", [
    "UPDATE jobs SET state='cancelled'",
    "UPDATE jobs SET attempt_no=2",
    "UPDATE cases SET lifecycle_round=lifecycle_round+1",
    "UPDATE broker_grants SET revoked_at='2026-09-08T00:29:00+00:00'",
    "UPDATE broker_grants SET expires_at='2026-09-08T00:30:00+00:00'",
])
def test_stale_renewal_has_no_writes(conn, change):
    request = setup(conn)
    conn.execute(change)
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        renew(conn, request, peer_uid=1234, now=datetime(2026, 9, 8, 0, 30, tzinfo=UTC))
    assert list(conn.iterdump()) == before
