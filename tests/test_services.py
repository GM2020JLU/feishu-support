from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from k3_support.config import Config, validate_config
from k3_support.db import connect, transaction
from k3_support.services import (
    JobHeartbeatError,
    JobLeaseHeartbeat,
    _job_worker_heartbeat,
    _outbox_tick,
    _renew_job_health,
    _start_job_lease_heartbeat,
    job_worker_main,
)
from k3_support.store import enqueue_outbox
from k3_support.watchdog import collect_health_alerts, record_health_alerts


def _active(config: Config) -> Config:
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    return Config(validate_config(raw), config.path)


def test_outbox_tick_reports_degraded_when_delivery_fails(conn, config):
    cfg = _active(config)
    with transaction(conn):
        enqueue_outbox(
            conn,
            channel="telegram",
            action_type="notify",
            destination="telegram:owner-chat",
            payload={"text": "test"},
            idempotency_key="service-delivery-error",
        )

    def fail(*_args, **_kwargs):
        raise RuntimeError("transport unavailable")

    result = _outbox_tick(conn, cfg, delivery=fail)

    assert result["ready"] is False
    assert result["error"] == "RuntimeError: transport unavailable"
    assert (
        conn.execute(
            "SELECT status FROM service_state WHERE component='outbox'"
        ).fetchone()[0]
        == "degraded"
    )


def _running_job(conn, *, now=None, worker_id="worker-1"):
    observed = now or datetime.now(UTC)
    conn.execute(
        """INSERT INTO jobs(job_id,job_type,state,lease_owner,lease_expires_at,
               heartbeat_at,input_digest,attempt_no,available_at,created_at,updated_at)
           VALUES('job_health','codex','running',?,?,?,'health-input',1,?,?,?)""",
        (
            worker_id,
            (observed + timedelta(minutes=2)).isoformat(),
            observed.isoformat(),
            observed.isoformat(),
            observed.isoformat(),
            observed.isoformat(),
        ),
    )
    _job_worker_heartbeat(
        conn,
        worker_id,
        "ready",
        {
            "job_id": "job_health",
            "attempt_no": 1,
            "heartbeat_phase": "starting",
        },
        register=True,
        now=observed,
    )


def _job_alerts(conn, config, now):
    return [
        alert
        for alert in collect_health_alerts(conn, config, now=now)
        if "job_worker" in alert["key"]
    ]


def test_15_minute_job_renews_service_health_and_death_is_distinct(conn, config):
    start = datetime(2026, 9, 7, 1, tzinfo=UTC)
    _running_job(conn, now=start)
    renewing = connect(config.database_path)
    try:
        for interval in range(1, 31):
            observed = start + timedelta(seconds=30 * interval)
            assert (
                _renew_job_health(
                    renewing,
                    job_id="job_health",
                    worker_id="worker-1",
                    attempt_no=1,
                    now=observed,
                )
                == "running"
            )
            assert _job_alerts(conn, config, observed) == []
        health = json.loads(
            conn.execute(
                "SELECT detail_json FROM service_state WHERE component='job_worker'"
            ).fetchone()[0]
        )
        assert health["job_type"] == "codex"
        assert health["job_state"] == "running"
        dead_at = observed + timedelta(minutes=4)
        alerts = _job_alerts(conn, config, dead_at)
        assert [alert["key"] for alert in alerts] == ["heartbeat_stale:job_worker"]
        assert len(record_health_alerts(conn, alerts, now=dead_at)["new"]) == 1
        assert (
            record_health_alerts(
                conn,
                _job_alerts(conn, config, dead_at + timedelta(minutes=10)),
                now=dead_at + timedelta(minutes=10),
            )["new"]
            == []
        )
        _job_worker_heartbeat(
            conn,
            "worker-restarted",
            "ready",
            {"heartbeat_phase": "idle"},
            register=True,
            now=dead_at + timedelta(minutes=11),
        )
        assert _job_alerts(conn, config, dead_at + timedelta(minutes=11)) == []
        assert record_health_alerts(conn, [], now=dead_at + timedelta(minutes=11))[
            "cleared"
        ] == ["heartbeat_stale:job_worker"]
    finally:
        renewing.close()


@pytest.mark.parametrize("loss", ["owner", "attempt", "expiry", "cancel"])
def test_lost_or_expired_claim_cannot_be_renewed_or_claim_healthy(conn, config, loss):
    now = datetime.now(UTC)
    _running_job(conn, now=now)
    changes = {
        "owner": ("lease_owner", "successor"),
        "attempt": ("attempt_no", 2),
        "expiry": ("lease_expires_at", (now - timedelta(seconds=1)).isoformat()),
        "cancel": ("state", "cancelled"),
    }
    column, value = changes[loss]
    conn.execute(f"UPDATE jobs SET {column}=? WHERE job_id='job_health'", (value,))
    before = dict(
        conn.execute("SELECT * FROM jobs WHERE job_id='job_health'").fetchone()
    )
    with pytest.raises(JobHeartbeatError, match="lost_job_claim"):
        _renew_job_health(
            conn, job_id="job_health", worker_id="worker-1", attempt_no=1, now=now
        )
    assert (
        dict(conn.execute("SELECT * FROM jobs WHERE job_id='job_health'").fetchone())
        == before
    )
    assert [alert["key"] for alert in _job_alerts(conn, config, now)] == [
        "job_worker_claim_invalid"
    ]


def test_old_worker_and_previous_job_cannot_overwrite_new_service_health(conn, config):
    _running_job(conn)
    _job_worker_heartbeat(
        conn, "successor", "ready", {"heartbeat_phase": "idle"}, register=True
    )
    before = dict(
        conn.execute(
            "SELECT * FROM service_state WHERE component='job_worker'"
        ).fetchone()
    )
    monitor = _start_job_lease_heartbeat(
        config.database_path, job_id="job_health", worker_id="worker-1"
    )
    assert monitor.failed.is_set()
    assert monitor.reason == "superseded_worker"
    monitor()
    assert (
        dict(
            conn.execute(
                "SELECT * FROM service_state WHERE component='job_worker'"
            ).fetchone()
        )
        == before
    )
    assert _job_worker_heartbeat(conn, "worker-1", "ready", {}) is False


def test_completed_job_continues_review_health_without_reviving_lease(conn, config):
    now = datetime.now(UTC)
    _running_job(conn, now=now)
    conn.execute(
        "UPDATE jobs SET state='succeeded',lease_owner=NULL,lease_expires_at=NULL WHERE job_id='job_health'"
    )
    assert (
        _renew_job_health(
            conn,
            job_id="job_health",
            worker_id="worker-1",
            attempt_no=1,
            now=now + timedelta(minutes=10),
        )
        == "finishing"
    )
    assert (
        conn.execute(
            "SELECT lease_expires_at FROM jobs WHERE job_id='job_health'"
        ).fetchone()[0]
        is None
    )
    assert _job_alerts(conn, config, now + timedelta(minutes=10)) == []


def test_heartbeat_thread_failure_is_observable_and_does_not_leak_exception_text(
    conn, config, monkeypatch
):
    _running_job(conn)
    original = _renew_job_health
    calls = 0

    def renew(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("private debug content")
        return original(*args, **kwargs)

    monkeypatch.setattr("k3_support.services._renew_job_health", renew)
    monitor = _start_job_lease_heartbeat(
        config.database_path,
        job_id="job_health",
        worker_id="worker-1",
        interval_seconds=0.01,
    )
    assert monitor.failed.wait(timeout=1)
    monitor()
    health = conn.execute(
        "SELECT status,detail_json FROM service_state WHERE component='job_worker'"
    ).fetchone()
    assert health["status"] == "degraded"
    assert monitor.reason == "heartbeat_error"
    assert monitor.error_class == "RuntimeError"
    assert "private debug content" not in health["detail_json"]


def test_job_worker_failure_stays_degraded_until_process_recovery(
    conn, config, monkeypatch
):
    monitor = JobLeaseHeartbeat()
    monitor.reason, monitor.error_class = "heartbeat_error", "RuntimeError"
    monitor.failed.set()
    claims = []
    outputs = []

    def claim(*args, **kwargs):
        claims.append(kwargs)
        return [{"job_id": "job_stopped", "attempt_no": 1}]

    def run(_component, tick, _interval):
        outputs.extend([tick(conn, config), tick(conn, config)])

    monkeypatch.setattr("k3_support.services.claim_jobs", claim)
    monkeypatch.setattr(
        "k3_support.services._start_job_lease_heartbeat",
        lambda *args, **kwargs: monitor,
    )
    monkeypatch.setattr("k3_support.services._run", run)
    job_worker_main()
    assert len(claims) == 1
    assert [output["ready"] for output in outputs] == [False, False]
    assert (
        conn.execute(
            "SELECT status FROM service_state WHERE component='job_worker'"
        ).fetchone()[0]
        == "degraded"
    )
