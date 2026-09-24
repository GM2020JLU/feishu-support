"""Read-only readiness facts, not an authority or an activation command."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .config import Config, ConfigError, validate_config
from .db import migration_files
from .knowledge_release import verify_release
from .timeutil import parse_iso
from .watchdog import collect_health_alerts


def _condition(category, code, reason, next_step):
    return {
        "category": category,
        "code": code,
        "reason": reason,
        "next_step": next_step,
        "applies_to": "automatic_knowledge_replies"
        if category == "knowledge_release"
        else "active_operations",
    }


def _health(conn, config, observed):
    alerts, schema, queues = [], {}, {}
    try:
        available = {version for version, _, _ in migration_files()}
        applied = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        }
        schema = {
            "missing": sorted(available - applied),
            "unsupported": sorted(applied - available),
        }
        if schema["missing"] or schema["unsupported"]:
            alerts.append({"key": "schema_release_mismatch", "detail": schema})
        # Do not treat legacy article inventory as either health or quality proof.
        alerts.extend(
            alert
            for alert in collect_health_alerts(conn, config, now=observed)
            if alert["key"] != "approved_knowledge_empty"
        )
        for table, column in (
            ("inbound_events", "status"),
            ("jobs", "state"),
            ("outbox", "state"),
        ):
            queues[table] = {
                row[0]: row[1]
                for row in conn.execute(
                    f"SELECT {column},count(*) FROM {table} GROUP BY {column}"
                )
            }
        actionable = next(
            (
                alert["detail"]["outbox_ids"]
                for alert in alerts
                if alert["key"] == "outbox_permanent_failures"
            ),
            [],
        )
        uncertain = conn.execute(
            """SELECT count(*) FROM outbox o JOIN json_each(?) active ON active.value=o.outbox_id
               WHERE (json_extract(o.remote_result_json,'$.error_type')='uncertain_delivery'
                      OR EXISTS(SELECT 1 FROM outbox_attempt_events e
                                WHERE e.claim_token=o.claim_token AND e.event_type='uncertain'))
                 AND NOT EXISTS(SELECT 1 FROM outbox_attempt_events e
                                WHERE e.claim_token=o.claim_token AND e.event_type='delivered')""",
            (json.dumps(actionable),),
        ).fetchone()[0]
        if uncertain:
            alerts.append(
                {"key": "outbox_unknown_outcomes", "detail": {"count": uncertain}}
            )
        return {
            "state": "degraded" if alerts else "healthy",
            "alerts": alerts,
            "schema": schema,
            "queue_counts": queues,
            "basis": "local SQLite/service/backup observations only; no external probe",
        }
    except (sqlite3.Error, OSError, TypeError, ValueError, KeyError) as exc:
        return {
            "state": "unknown",
            "alerts": alerts,
            "schema": schema,
            "queue_counts": queues,
            "observation_error": type(exc).__name__,
            "basis": "local observation incomplete",
        }


def _control_snapshot(conn, observed):
    """Do not call runtime-control helpers which initialize/expire stored state."""
    try:
        row = conn.execute(
            "SELECT mode,revision,outbound_fence,auto_expires_at FROM global_control_state WHERE scope='feishu_support'"
        ).fetchone()
        if row is None:
            return {
                "state": "not_initialized",
                "stored_mode": None,
                "effective_observed_mode": None,
            }
        expires = row["auto_expires_at"]
        expired = row["mode"] == "auto_60" and parse_iso(expires) <= observed
        return {
            "state": "observed",
            "stored_mode": row["mode"],
            "revision": row["revision"],
            "outbound_fence": row["outbound_fence"],
            "auto_expires_at": expires,
            "expired": expired,
            "effective_observed_mode": "collaborate" if expired else row["mode"],
            "materialized": False,
        }
    except (sqlite3.Error, TypeError, ValueError, KeyError) as exc:
        return {"state": "unknown", "observation_error": type(exc).__name__}


def _knowledge(conn, config, observed, actual_runtime_binding):
    # Artifact validity and current-runtime equality are deliberately separate.
    artifact = verify_release(conn, config, now=observed)
    artifact_verified = artifact.get("artifact_verified") is True
    current = None
    if actual_runtime_binding is not None:
        try:
            if not isinstance(actual_runtime_binding, dict):
                raise TypeError("runtime descriptor must be a mapping")
            binding = json.loads(json.dumps(actual_runtime_binding, allow_nan=False))
            current = verify_release(
                conn, config, runtime_binding=binding, now=observed
            )
        except (TypeError, ValueError):
            current = {"ready": False, "reason": "invalid_actual_runtime_descriptor"}
    human_evidence = artifact.get("evidence_class") == "human_reviewed"
    same_release = current is not None and current.get(
        "release_digest"
    ) == artifact.get("release_digest")
    current_verified = (
        artifact_verified
        and human_evidence
        and current is not None
        and current.get("ready") is True
        and current.get("artifact_verified") is True
        and current.get("evidence_class") == "human_reviewed"
        and same_release
    )
    if current_verified:
        state, reason = "current_runtime_verified", None
    elif artifact_verified and not human_evidence:
        state, reason = "fixture_or_unverified_evidence", "human_gold_not_verified"
    elif artifact_verified and current is None:
        state, reason = (
            "artifact_verified_current_runtime_unknown",
            "current_runtime_not_observed",
        )
    elif artifact_verified and current.get("ready") is True and not same_release:
        state, reason = "current_runtime_blocked", "release_changed_during_observation"
    elif artifact_verified:
        state, reason = "current_runtime_blocked", current.get("reason")
    else:
        state, reason = "blocked", artifact.get("reason") or "release_not_verified"
    return {
        "state": state,
        "artifact_verified": artifact_verified,
        "current_runtime_verified": current_verified,
        "reason": reason,
        "release_id": artifact.get("release_id"),
        "release_digest": artifact.get("release_digest"),
        "expires_at": artifact.get("expires_at"),
        "evidence_class": artifact.get("evidence_class"),
        "human_gold_verified": artifact_verified and human_evidence,
        "sample_size": artifact.get("sample_size"),
        "actual_runtime_descriptor_supplied": actual_runtime_binding is not None,
        "descriptor_source": "explicit_actual_observation"
        if actual_runtime_binding is not None
        else "not_observed",
    }


def readiness_report(
    conn: sqlite3.Connection,
    config: Config,
    *,
    actual_runtime_binding: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Inspect three independent axes without dispatch, migrations or mode writes.

    ``actual_runtime_binding`` must come from an actual invocation by the caller;
    this reporter never extracts one from a historical route or signed artifact.
    Even a verified knowledge release is not an OS execution-boundary proof.
    """
    observed = now or datetime.now(UTC)
    if observed.tzinfo is None:
        raise ValueError("readiness time must include a timezone")
    observed = observed.astimezone(UTC)
    raw = config.raw if isinstance(config.raw, dict) else {}
    features = raw.get("features", {})
    if not isinstance(features, dict):
        features = {}
    blockers = []
    try:
        validated = Config(validate_config(copy.deepcopy(config.raw)), config.path)
        config_valid, config_error = True, None
    except (ConfigError, TypeError, ValueError, KeyError) as exc:
        validated = None
        config_valid, config_error = False, type(exc).__name__
        blockers.append(
            _condition(
                "runtime_health",
                "invalid_configuration",
                "配置未通过当前 schema 校验。",
                "修复配置或执行经审核的兼容迁移；本报告不会改写配置。",
            )
        )
    if validated:
        from .feature_settings import observation

        feature_state = observation(validated)
        features = feature_state["values"]
        if feature_state["state"] == "unavailable":
            blockers.append(
                _condition(
                    "authority_boundary",
                    "feature_settings_unavailable",
                    feature_state["message"],
                    "核对基础配置与功能覆盖版本及数据库完整性；不要通过删除覆盖记录绕过检查。",
                )
            )
    else:
        feature_state = {"state": "unavailable", "revision": None}
    health = (
        _health(conn, validated, observed)
        if validated
        else {"state": "unknown", "alerts": [], "basis": "configuration invalid"}
    )
    if health["state"] != "healthy":
        if health["alerts"]:
            for alert in health["alerts"]:
                blockers.append(
                    _condition(
                        "runtime_health",
                        alert["key"],
                        alert.get("message") or "本地运行检查未通过。",
                        "检查对应服务日志/租约、数据恢复或备份；避免仅清空队列掩盖原因。",
                    )
                )
        else:
            blockers.append(
                _condition(
                    "runtime_health",
                    "health_observation_incomplete",
                    "运行健康尚未完整核实。",
                    "先核对配置、数据库版本与可读性，再重新运行只读检查。",
                )
            )
    authority = {
        "state": "unproven",
        "config_valid": config_valid,
        "config_error": config_error,
        "feature_settings": {key: feature_state[key] for key in ("state", "revision")},
        "configured_mode": raw.get("mode"),
        "global_control": _control_snapshot(conn, observed),
        "enabled_capabilities": sorted(
            key for key, value in features.items() if value is True
        ),
        "os_isolation_verified": False,
        "checks": [
            {
                "id": "same_uid_control_plane",
                "status": "not_independently_verified",
                "detail": "SQLite 状态、审批字符串和应用级 fence 不是同 UID 进程间的 OS 权限隔离证明。",
            },
            {
                "id": "per_action_authority",
                "status": "required_at_execution",
                "detail": "board 会话占用、WIP push、会议创建等仍须各自审批和执行前校验；模式或本报告不替代审批。",
            },
        ],
    }
    blockers.append(
        _condition(
            "authority_boundary",
            "os_authority_isolation_unproven",
            "本报告没有独立的控制面/编码代理 OS 权限隔离证据。",
            "由部署管理员验证独立身份、凭据/签名钥匙隔离及绕过测试，保留实际环境证据；单元测试不替代此验收。",
        )
    )
    if validated:
        try:
            knowledge = _knowledge(conn, validated, observed, actual_runtime_binding)
        except (sqlite3.Error, OSError, TypeError, ValueError, KeyError) as exc:
            knowledge = {
                "state": "unknown",
                "artifact_verified": False,
                "current_runtime_verified": False,
                "reason": "release_observation_failed",
                "observation_error": type(exc).__name__,
            }
    else:
        knowledge = {
            "state": "unknown",
            "artifact_verified": False,
            "current_runtime_verified": False,
            "reason": "invalid_configuration",
        }
    if not knowledge["current_runtime_verified"]:
        reason = knowledge.get("reason") or "knowledge_release_not_ready"
        if reason == "current_runtime_not_observed":
            step = "通过实际 live/evaluation 同一查询入口观察运行描述符，再核对已签发布；不要从旧 route 或配置默认模型推测。"
        elif knowledge.get("artifact_verified"):
            step = "使用真人审核 Gold 与实际查询结果复核模型/索引/降级路径，重新匹配经独立签名的发布；fixture 不可晋级生产质量。"
        else:
            step = "配置代理不可写、root-owned 的独立 trust policy 与有效签名发布；完成真人 Gold 审核及实际运行质量门槛。"
        blockers.append(
            _condition(
                "knowledge_release", reason, "知识发布或当前运行质量尚未验证。", step
            )
        )
    return {
        "schema_version": 1,
        "checked_at": observed.isoformat(),
        "read_only": True,
        "runtime_health": health,
        "authority_boundary": authority,
        "knowledge_release": knowledge,
        "active_ready": False,
        "activation_performed": False,
        "blockers": blockers,
        "next_steps": list(dict.fromkeys(item["next_step"] for item in blockers)),
        "summary": "运行健康、执行授权与知识质量分别报告；绿色心跳、空队列或 approved 数量均不授予 Active 权限。",
    }
