from __future__ import annotations

from datetime import UTC, datetime, timedelta

from k3_support.db import transaction
from k3_support.services import _job_worker_heartbeat
from k3_support.store import enqueue_outbox
from k3_support.watchdog import collect_health_alerts, record_health_alerts


def test_body_retention_health_required_only_when_policy_enabled(conn, config):
    from test_routing import active_config
    from k3_support.operations import heartbeat
    cfg = active_config(config)
    assert not any('body_retention' in alert['key'] for alert in collect_health_alerts(conn, cfg))
    cfg.raw['policy']['body_retention_days'] = 30
    assert any(alert['key'] == 'heartbeat_missing:body_retention' for alert in collect_health_alerts(conn, cfg))
    heartbeat(conn, 'body_retention', 'ready', {'reason': 'global_mode_hold', 'cleared': 0})
    assert not any('body_retention' in alert['key'] for alert in collect_health_alerts(conn, cfg))
    conn.execute("UPDATE service_state SET heartbeat_at=? WHERE component='body_retention'",
                 ((datetime.now(UTC) - timedelta(minutes=4)).isoformat(),))
    assert any('body_retention' in alert['key'] for alert in collect_health_alerts(conn, cfg))


def _alert(key: str, fingerprint: str, message: str = "problem") -> dict:
    return {
        "key": key,
        "fingerprint": fingerprint,
        "message": message,
        "detail": {"fingerprint": fingerprint},
    }


def test_unchanged_health_condition_notifies_only_once(conn):
    now = datetime.now(UTC)
    alert = _alert("outbox_permanent_failures", "two-failures")

    first = record_health_alerts(conn, [alert], now=now)
    repeated = record_health_alerts(conn, [alert], now=now + timedelta(minutes=10))

    assert first["new"] == [alert]
    assert repeated["new"] == []
    row = conn.execute(
        "SELECT status,last_notified_at,last_seen_at FROM health_alert_state"
    ).fetchone()
    assert row["status"] == "active"
    assert row["last_notified_at"] == now.isoformat()
    assert row["last_seen_at"] == (now + timedelta(minutes=10)).isoformat()


def test_changed_or_recurring_condition_notifies_again(conn):
    now = datetime.now(UTC)
    first = _alert("outbox_permanent_failures", "two-failures")
    changed = _alert("outbox_permanent_failures", "three-failures")

    record_health_alerts(conn, [first], now=now)
    changed_result = record_health_alerts(
        conn, [changed], now=now + timedelta(minutes=10)
    )
    cleared = record_health_alerts(conn, [], now=now + timedelta(minutes=20))
    recurring = record_health_alerts(conn, [changed], now=now + timedelta(minutes=30))

    assert changed_result["new"] == [changed]
    assert cleared["cleared"] == ["outbox_permanent_failures"]
    assert recurring["new"] == [changed]


def test_baseline_records_existing_conditions_without_notification(conn):
    now = datetime.now(UTC)
    alert = _alert("outbox_permanent_failures", "historical-failures")

    baseline = record_health_alerts(conn, [alert], now=now, baseline=True)
    next_run = record_health_alerts(conn, [alert], now=now + timedelta(minutes=10))

    assert baseline["new"] == []
    assert next_run["new"] == []
    assert (
        conn.execute("SELECT last_notified_at FROM health_alert_state").fetchone()[0]
        is None
    )


def test_disabled_urgent_failures_do_not_page_health_watchdog(conn, config):
    with transaction(conn):
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="feishu_urgent_sms",
            action_type="urgent",
            destination="om_alert",
            payload={"user_id_list": ["ou_owner"]},
            idempotency_key="disabled-sms-failure",
        )
    conn.execute(
        "UPDATE outbox SET state='permanent_failure' WHERE outbox_id=?",
        (outbox_id,),
    )

    keys = {alert["key"] for alert in collect_health_alerts(conn, config)}
    assert "outbox_permanent_failures" not in keys

    config.raw["notifications"]["feishu_sms_urgent"] = True
    keys = {alert["key"] for alert in collect_health_alerts(conn, config)}
    assert "outbox_permanent_failures" in keys


def test_enabled_auto_faq_without_approved_knowledge_is_unhealthy(conn, config):
    config.raw["mode"] = "active"
    config.raw["features"]["auto_faq"] = True
    config.raw["identity"]["feishu_owner_open_id"] = "ou_owner"

    keys = {alert["key"] for alert in collect_health_alerts(conn, config)}

    assert "approved_knowledge_empty" in keys


def test_job_health_failure_progress_and_recovery_do_not_repeat_notifications(
    conn, config
):
    now = datetime.now(UTC)
    initial = {
        "job_id": "job_one",
        "heartbeat_phase": "heartbeat_error",
        "error_class": "RuntimeError",
    }
    _job_worker_heartbeat(
        conn, "worker-one", "degraded", initial, register=True, now=now
    )

    def alerts(at):
        return [
            item
            for item in collect_health_alerts(conn, config, now=at)
            if "job_worker" in item["key"]
        ]

    first = alerts(now)
    assert [item["key"] for item in first] == ["component_unhealthy:job_worker"]
    assert len(record_health_alerts(conn, first, now=now)["new"]) == 1
    # The helper and the main loop describe the same failure with the same
    # stable fingerprint, despite fresh timestamps and the enclosing phase.
    later = now + timedelta(minutes=10)
    _job_worker_heartbeat(
        conn,
        "worker-one",
        "degraded",
        {
            "job_id": "job_one",
            "heartbeat_phase": "degraded",
            "heartbeat_error": {
                "reason": "heartbeat_error",
                "error_class": "RuntimeError",
            },
        },
        now=later,
    )
    assert record_health_alerts(conn, alerts(later), now=later)["new"] == []
    recovered = later + timedelta(minutes=1)
    _job_worker_heartbeat(
        conn,
        "worker-restarted",
        "ready",
        {"heartbeat_phase": "idle"},
        register=True,
        now=recovered,
    )
    assert alerts(recovered) == []
    assert record_health_alerts(conn, [], now=recovered)["cleared"] == [
        "component_unhealthy:job_worker"
    ]
