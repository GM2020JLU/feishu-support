from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .db import connect, integrity, migrate, transaction
from .ids import canonical_json, digest
from .timeutil import parse_iso

DEFAULT_CONFIG = Path("~/.hermes/k3-support/config.yaml").expanduser()


def _alert(key: str, message: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": key,
        "message": message,
        "detail": detail,
        "fingerprint": digest({"key": key, "detail": detail}),
    }


def actionable_permanent_outbox_ids(
    conn: sqlite3.Connection, config: Config
) -> list[str]:
    """Return failures that still belong to an enabled notification route."""
    disabled_channels = {
        channel
        for channel, notification in (
            ("feishu_urgent_app", "feishu_app_urgent"),
            ("feishu_urgent_sms", "feishu_sms_urgent"),
        )
        if not config.notification(notification)
    }
    return [
        str(row["outbox_id"])
        for row in conn.execute(
            """SELECT outbox_id,channel FROM outbox
                 WHERE state='permanent_failure' ORDER BY outbox_id"""
        )
        if str(row["channel"]) not in disabled_channels
    ]


def collect_health_alerts(
    conn: sqlite3.Connection,
    config: Config,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Collect stable health conditions without deciding whether to notify."""
    observed = now or datetime.now(UTC)
    alerts: list[dict[str, Any]] = []
    if not integrity(conn)["ok"]:
        alerts.append(_alert("database_integrity", "SQLite integrity check failed", {}))

    heartbeat_cutoff = observed - timedelta(minutes=3)
    required = {"ingress_poll", "worker", "job_worker", "outbox"}
    from .retention_settings import snapshot as retention_snapshot
    retention = retention_snapshot(conn, config)
    if retention['needs_migration']:
        alerts.append(_alert('body_retention_policy_changed', 'Body retention policy needs explicit rebase', {}))
    if config.mode == 'active' and retention['days'] is not None:
        required.add('body_retention')
    draft_retention = retention_snapshot(conn, config, scope='draft')
    if draft_retention['needs_migration']:
        alerts.append(_alert('draft_retention_policy_changed', 'Draft retention policy needs explicit rebase', {}))
    if config.mode == 'active' and draft_retention['days'] is not None:
        required.add('draft_retention')
    if config.feature("mail"):
        required.add("ingress_mail")
    rows = {
        str(row["component"]): row
        for row in conn.execute("SELECT * FROM service_state")
    }
    supervisor = rows.get("job_worker")
    if supervisor and json.loads(supervisor["detail_json"]).get("heartbeat_phase") == "supervising":
        required.update({"job_worker:query", "job_worker:debug", "job_worker:sync"})
    for component in sorted(required):
        row = rows.get(component)
        if row is None:
            alerts.append(
                _alert(
                    f"heartbeat_missing:{component}",
                    f"{component} heartbeat is missing",
                    {"component": component},
                )
            )
            continue
        if parse_iso(row["heartbeat_at"]) < heartbeat_cutoff:
            alerts.append(
                _alert(
                    f"heartbeat_stale:{component}",
                    f"{component} heartbeat is older than 3 minutes",
                    {"component": component},
                )
            )
            if component.startswith("job_worker"):
                # A dead heartbeat is one condition, not a second alert for
                # the last status it happened to publish before dying.
                continue
        if row["status"] != "ready":
            detail = {"component": component, "status": row["status"]}
            if component.startswith("job_worker"):
                health = json.loads(row["detail_json"])
                failure = health.get("heartbeat_error") or {}
                detail.update(
                    {
                        "job_id": health.get("job_id"),
                        "reason": failure.get("reason")
                        or health.get("heartbeat_phase"),
                        "error_class": failure.get("error_class")
                        or health.get("error_class"),
                    }
                )
            alerts.append(
                _alert(
                    f"component_unhealthy:{component}",
                    f"{component} status is {row['status']}",
                    detail,
                )
            )
            if component.startswith("job_worker"):
                continue
        if component.startswith("job_worker"):
            health = json.loads(row["detail_json"])
            if health.get("worker_id") and health.get("heartbeat_phase") in {
                "starting",
                "running",
                "finishing",
            }:
                job = conn.execute(
                    "SELECT state,lease_owner,lease_expires_at,heartbeat_at,attempt_no FROM jobs WHERE job_id=?",
                    (health.get("job_id"),),
                ).fetchone()
                valid = job is not None and job["attempt_no"] == health.get(
                    "attempt_no"
                )
                if valid and job["state"] == "running":
                    valid = (
                        job["lease_owner"] == health["worker_id"]
                        and job["lease_expires_at"] is not None
                        and parse_iso(job["lease_expires_at"]) > observed
                        and job["heartbeat_at"] is not None
                        and parse_iso(job["heartbeat_at"]) >= heartbeat_cutoff
                    )
                elif valid:
                    valid = job["state"] in {"succeeded", "failed"} and job[
                        "lease_owner"
                    ] in {None, health["worker_id"]}
                if not valid:
                    alerts.append(
                        _alert(
                            f"{component}_claim_invalid",
                            "job_worker heartbeat no longer matches a live claim or its completed result",
                            {
                                "component": component,
                                "job_id": health.get("job_id"),
                                "reason": "lost_job_claim",
                            },
                        )
                    )

    bot = rows.get("ingress_bot")
    if bot is None or bot["status"] != "ready":
        alerts.append(
            _alert(
                "ingress_bot_unavailable",
                "ingress_bot event stream is not ready",
                {"status": bot["status"] if bot else "missing"},
            )
        )
    reconcile = rows.get("reconcile")
    if (
        reconcile is None
        or parse_iso(reconcile["heartbeat_at"]) < observed - timedelta(minutes=15)
        or reconcile["status"] != "ready"
    ):
        alerts.append(
            _alert(
                "reconcile_unhealthy",
                "reconcile heartbeat/status is not healthy",
                {"status": reconcile["status"] if reconcile else "missing"},
            )
        )

    dead_letters = [
        str(row["event_pk"])
        for row in conn.execute(
            "SELECT event_pk FROM inbound_events WHERE status='dead_letter' ORDER BY event_pk"
        )
    ]
    if dead_letters:
        alerts.append(
            _alert(
                "inbox_dead_letters",
                f"Inbox dead letters: {len(dead_letters)}",
                {"event_ids": dead_letters},
            )
        )
    permanent = actionable_permanent_outbox_ids(conn, config)
    if permanent:
        alerts.append(
            _alert(
                "outbox_permanent_failures",
                f"Outbox permanent failures: {len(permanent)}",
                {"outbox_ids": permanent},
            )
        )

    from .delivery_recovery import unreconciled_blocked_deliveries

    unprojected = unreconciled_blocked_deliveries(conn)
    if unprojected:
        alerts.append(_alert(
            "delivery_handoff_missing", "Knowledge-blocked replies are missing their operator handoff",
            {"outbox_ids": [row["outbox_id"] for row in unprojected]},
        ))

    if config.feature("auto_faq"):
        approved_knowledge = conn.execute(
            "SELECT count(*) FROM knowledge_entries WHERE status='approved'"
        ).fetchone()[0]
        if approved_knowledge == 0:
            alerts.append(
                _alert(
                    "approved_knowledge_empty",
                    "auto_faq is enabled but approved knowledge is empty",
                    {},
                )
            )

    if config.feature("base_sync"):
        failed_base = [
            str(row["job_id"])
            for row in conn.execute(
                "SELECT job_id FROM jobs WHERE job_type='base_sync' AND state='failed' ORDER BY job_id"
            )
        ]
        if failed_base:
            alerts.append(
                _alert(
                    "base_sync_exhausted",
                    f"Base sync exhausted jobs: {len(failed_base)}",
                    {"job_ids": failed_base},
                )
            )
        oldest_base = conn.execute(
            """SELECT min(created_at) FROM jobs
               WHERE job_type='base_sync' AND state IN ('queued','running')"""
        ).fetchone()[0]
        if oldest_base and parse_iso(oldest_base) < observed - timedelta(minutes=30):
            alerts.append(
                _alert(
                    "base_sync_stale_backlog",
                    "Base sync backlog is older than 30 minutes",
                    {"oldest_created_at": oldest_base},
                )
            )

    backups = sorted(
        (
            path
            for path in (config.data_dir / "backups").glob("support-*.db")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not backups:
        alerts.append(_alert("backup_missing", "No SQLite backup exists", {}))
    else:
        newest = datetime.fromtimestamp(backups[0].stat().st_mtime, UTC)
        if newest < observed - timedelta(hours=36):
            alerts.append(
                _alert(
                    "backup_stale",
                    "Newest SQLite backup is older than 36 hours",
                    {"path": backups[0].name},
                )
            )
    return alerts


def record_health_alerts(
    conn: sqlite3.Connection,
    alerts: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    baseline: bool = False,
) -> dict[str, Any]:
    """Notify only for a new, changed, or recurring health condition."""
    observed = (now or datetime.now(UTC)).isoformat()
    current = {str(alert["key"]): alert for alert in alerts}
    new: list[dict[str, Any]] = []
    cleared: list[str] = []
    with transaction(conn):
        existing = {
            str(row["alert_key"]): row
            for row in conn.execute("SELECT * FROM health_alert_state")
        }
        for key, alert in current.items():
            previous = existing.get(key)
            changed = (
                previous is None
                or previous["status"] == "cleared"
                or previous["fingerprint"] != alert["fingerprint"]
            )
            notify = changed and not baseline
            if notify:
                new.append(alert)
            first_seen = (
                previous["first_seen_at"]
                if previous is not None
                and previous["status"] == "active"
                and previous["fingerprint"] == alert["fingerprint"]
                else observed
            )
            last_notified = (
                observed
                if notify
                else (previous["last_notified_at"] if previous is not None else None)
            )
            conn.execute(
                """INSERT INTO health_alert_state(alert_key,status,fingerprint,message,
                       detail_json,first_seen_at,last_seen_at,last_notified_at,cleared_at)
                   VALUES(?,'active',?,?,?,?,?,?,NULL)
                   ON CONFLICT(alert_key) DO UPDATE SET status='active',
                       fingerprint=excluded.fingerprint,message=excluded.message,
                       detail_json=excluded.detail_json,first_seen_at=excluded.first_seen_at,
                       last_seen_at=excluded.last_seen_at,
                       last_notified_at=excluded.last_notified_at,cleared_at=NULL""",
                (
                    key,
                    alert["fingerprint"],
                    alert["message"],
                    canonical_json(alert["detail"]),
                    first_seen,
                    observed,
                    last_notified,
                ),
            )
        for key, previous in existing.items():
            if previous["status"] == "active" and key not in current:
                conn.execute(
                    """UPDATE health_alert_state SET status='cleared',cleared_at=?,
                           last_seen_at=? WHERE alert_key=?""",
                    (observed, observed, key),
                )
                cleared.append(key)
    return {
        "current_count": len(current),
        "new": new,
        "cleared": cleared,
        "baseline": baseline,
    }


def evaluate_health(
    conn: sqlite3.Connection,
    config: Config,
    *,
    now: datetime | None = None,
    baseline: bool = False,
) -> dict[str, Any]:
    return record_health_alerts(
        conn,
        collect_health_alerts(conn, config, now=now),
        now=now,
        baseline=baseline,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="k3-support-health-watchdog")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="record current conditions without producing an alert",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    conn = connect(config.database_path)
    migrate(conn)
    result = evaluate_health(conn, config, baseline=args.baseline)
    if args.baseline:
        print(
            json.dumps(
                {
                    "baseline": True,
                    "current_count": result["current_count"],
                    "cleared": result["cleared"],
                },
                ensure_ascii=False,
            )
        )
    elif result["new"]:
        print(
            "K3 support health alert\n"
            + "\n".join(f"- {item['message']}" for item in result["new"])
        )


if __name__ == "__main__":
    main()
