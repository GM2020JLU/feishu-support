"""Operator feature settings; capabilities and exact approvals remain separate.

No file rewrite, process restart or network call. Readers use fresh committed
state so an existing worker observes changes at its next feature check.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from .config import FEATURES
from .db import transaction
from .ids import canonical_json, digest, new_id
from .timeutil import iso_now


def _values(value):
    if (
        not isinstance(value, dict)
        or set(value) != FEATURES
        or any(type(item) is not bool for item in value.values())
    ):
        raise ValueError("功能配置必须只包含完整的布尔开关")
    return value


def snapshot(conn, config):
    base = digest(config.raw)
    row = conn.execute("SELECT * FROM feature_settings WHERE singleton=1").fetchone()
    if row and row["base_digest"] != base:
        raise ValueError("基础配置已变化，请先核对配置版本；功能暂不放行")
    values = _values(json.loads(row["values_json"]) if row else config.raw["features"])
    return {
        "revision": row["revision"] if row else 0,
        "base_digest": base,
        "values": values,
    }


def observation(config):
    """Observe one committed settings snapshot without initializing anything.

    An unreadable policy is different from an intentional all-off policy.
    Error details never include database contents, paths or credentials.
    """
    baseline = {"state": "base", "revision": 0, "values": dict(config.raw["features"])}
    if not config.database_path.is_file():
        return baseline
    conn = None
    try:
        conn = sqlite3.connect(
            config.database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1
        )
        conn.row_factory = sqlite3.Row
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feature_settings'"
        ).fetchone():
            return baseline
        current = snapshot(conn, config)
        return {
            "state": "override" if current["revision"] else "base",
            "revision": current["revision"],
            "values": current["values"],
        }
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return {
            "state": "unavailable",
            "revision": None,
            "values": dict.fromkeys(FEATURES, False),
            "message": "功能配置不可核实，暂不放行；请核对基础配置版本和数据库完整性。",
        }
    finally:
        if conn is not None:
            conn.close()


def effective(config):
    """No initialization/migration on reads; damaged state fails closed."""
    return observation(config)["values"]


def _editor_state(conn, config):
    row = conn.execute("SELECT * FROM feature_settings WHERE singleton=1").fetchone()
    target = digest(config.raw)
    if row and row["base_digest"] != target:
        historical = _values(json.loads(row["values_json"]))
        return {
            "revision": row["revision"],
            "base_digest": target,
            "source_base_digest": row["base_digest"],
            "requires_rebase": True,
            "values": dict.fromkeys(FEATURES, False),
            "historical_values": historical,
        }
    return {
        **snapshot(conn, config),
        "source_base_digest": target,
        "requires_rebase": False,
    }


def preview(
    conn,
    config,
    *,
    session_id,
    values,
    expected_revision,
    rollback_revision=None,
    rebase=False,
):
    if not session_id:
        raise ValueError("需要已认证的控制台会话")
    with transaction(conn):
        current = _editor_state(conn, config)
        if type(rebase) is not bool or rebase != current["requires_rebase"]:
            raise ValueError("基础配置不一致时必须明确预览迁移；普通草稿不能自动迁移")
        if rebase and rollback_revision is not None:
            raise ValueError("基础迁移不能同时回滚历史配置")
        if (
            type(expected_revision) is not int
            or expected_revision != current["revision"]
        ):
            raise ValueError("配置已变化，请重新读取后编辑")
        if rollback_revision is not None:
            if (
                type(rollback_revision) is not int
                or rollback_revision < 1
                or values is not None
            ):
                raise ValueError("无效的回滚版本")
            old = conn.execute(
                "SELECT * FROM feature_settings_history WHERE revision=?",
                (rollback_revision,),
            ).fetchone()
            if not old or old["base_digest"] != current["base_digest"]:
                raise ValueError("该版本不存在或基础配置不一致")
            values = json.loads(old["previous_json"])
        _values(values)
        changes = [
            {"feature": key, "before": current["values"][key], "after": values[key]}
            for key in sorted(FEATURES)
            if current["values"][key] != values[key]
        ]
        if not changes and not rebase:
            raise ValueError("没有配置变更")
        identifier = new_id("fsd")
        expires = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
        conn.execute(
            "INSERT INTO feature_settings_drafts(draft_id,session_id,base_revision,base_digest,previous_json,values_json,expires_at,created_at,source_base_digest) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                session_id,
                current["revision"],
                current["base_digest"],
                canonical_json(current["values"]),
                canonical_json(values),
                expires,
                iso_now(),
                current["source_base_digest"],
            ),
        )
    return {
        "draft_id": identifier,
        "changes": changes,
        "expires_at": expires,
        "base_revision": current["revision"],
        "applied": False,
        "rebase": rebase,
        "base_change": {
            "from": current["source_base_digest"],
            "to": current["base_digest"],
        }
        if rebase
        else None,
        "warning": (
            "这是基础配置迁移：当前功能因版本不一致而停用，旧开关不会自动继承。仅将所选开关绑定到当前基础配置；不修改基础文件，也不保证旧进程已升级。\n"
            if rebase
            else ""
        )
        + "应用后下次功能检查采用新设置。开启可能允许已有待办继续；全局模式、独立审批和任务接管仍有效。关闭不会撤回已发送内容或立即终止在途任务。",
    }


def apply(conn, config, *, session_id, actor_id, draft_id):
    if not actor_id or not session_id:
        raise ValueError("需要操作员身份")
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM feature_settings_drafts WHERE draft_id=? AND session_id=?",
            (draft_id, session_id),
        ).fetchone()
        if not row:
            raise ValueError("草稿不属于当前会话，请重新预览")
        if row["applied_revision"] is not None:
            return {
                "revision": row["applied_revision"],
                "applied": True,
                "replayed": True,
            }
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
            raise ValueError("草稿已过期，请重新预览")
        current = _editor_state(conn, config)
        if (
            row["base_revision"] != current["revision"]
            or row["base_digest"] != current["base_digest"]
            or (row["source_base_digest"] or row["base_digest"])
            != current["source_base_digest"]
        ):
            raise ValueError("配置已变化，旧草稿不能覆盖新配置")
        _values(json.loads(row["values_json"]))
        revision, now = current["revision"] + 1, iso_now()
        conn.execute(
            "INSERT INTO feature_settings VALUES(1,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,base_digest=excluded.base_digest,values_json=excluded.values_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (revision, current["base_digest"], row["values_json"], actor_id, now),
        )
        conn.execute(
            "INSERT INTO feature_settings_history VALUES(?,?,?,?,?,?,?)",
            (
                revision,
                current["base_digest"],
                row["values_json"],
                row["previous_json"],
                draft_id,
                actor_id,
                now,
            ),
        )
        conn.execute(
            "UPDATE feature_settings_drafts SET applied_revision=? WHERE draft_id=?",
            (revision, draft_id),
        )
    return {"revision": revision, "applied": True, "replayed": False}


def view(conn, config):
    return {
        **_editor_state(conn, config),
        "history": [
            dict(row)
            for row in conn.execute(
                "SELECT revision,updated_by,updated_at FROM feature_settings_history ORDER BY revision DESC LIMIT 20"
            )
        ],
        "scope": "功能开关；不会修改授权、模型、工作时间或基础配置文件",
    }
