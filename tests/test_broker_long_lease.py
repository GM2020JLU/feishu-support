from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_broker_start import setup
from test_review import active_config

from k3_support.broker_renew import renew
from k3_support.broker_start import authorize

NOW = datetime(2026, 9, 8, 0, 30, tzinfo=UTC)


def started(conn, config):
    request = setup(conn, config)
    request['method'] = 'renew'
    cfg = active_config(config)
    authorize(conn, cfg, {**request, "method": "start"}, peer_uid=1234, now=NOW)
    request["request_id"] = str(uuid4())
    return request, cfg


def test_started_job_heartbeat_advances_both_leases_once(conn, config):
    request, cfg = started(conn, config)
    conn.execute("UPDATE jobs SET lease_expires_at=?", ((NOW+timedelta(seconds=60)).isoformat(),))
    at = NOW + timedelta(seconds=30)
    result = renew(conn, request, peer_uid=1234, config=cfg, now=at)
    assert result["expires_at"] == (at+timedelta(seconds=120)).isoformat()
    assert conn.execute("SELECT lease_expires_at FROM jobs").fetchone()[0] == result["expires_at"]
    before = list(conn.iterdump())
    assert renew(conn, request, peer_uid=1234, config=cfg, now=at+timedelta(seconds=10)) == result
    assert list(conn.iterdump()) == before


def test_total_lifetime_has_hard_boundary(conn, config):
    request, cfg = started(conn, config)
    end = NOW + timedelta(seconds=7200)
    # Synthetic long-lived preexisting leases isolate the total-lifetime gate.
    conn.execute("UPDATE jobs SET lease_expires_at=?", ((end+timedelta(minutes=10)).isoformat(),))
    conn.execute("UPDATE broker_grants SET expires_at=?", ((end+timedelta(minutes=10)).isoformat(),))
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="lifetime"):
        renew(conn, request, peer_uid=1234, config=cfg, now=end)
    assert list(conn.iterdump()) == before
    result = renew(conn, request, peer_uid=1234, config=cfg, now=end-timedelta(seconds=1))
    assert result["expires_at"] == end.isoformat()
