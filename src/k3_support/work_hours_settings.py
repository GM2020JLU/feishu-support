"""One committed daily-hours policy shared by scheduling consumers."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from .db import transaction
from .ids import canonical_json, digest, new_id
from .timeutil import iso_now, parse_iso


def validate(values):
    from .config import _clock_minutes

    if not isinstance(values, dict) or set(values) != {"start", "end"}:
        raise ValueError("工作时间必须包含 start/end")
    if _clock_minutes(values["start"]) == _clock_minutes(values["end"]):
        raise ValueError("开始和结束时间不能相同")
    return dict(values)


def snapshot(conn, config, *, allow_mismatch=False):
    base = digest(
        {"hours": config.raw["work_hours"], "timezone": config.raw["timezone"]}
    )
    row = conn.execute("SELECT * FROM work_hours_settings WHERE singleton=1").fetchone()
    mismatch = bool(row and row["base_digest"] != base)
    if mismatch and not allow_mismatch:
        raise ValueError("基础工作时间或时区已变化，不能沿用旧覆盖")
    return {
        "revision": row["revision"] if row else 0,
        "base_digest": base,
        "values": validate(
            json.loads(row["values_json"]) if row else config.raw["work_hours"]
        ),
        "timezone": config.raw["timezone"],
        "needs_migration": mismatch,
        "stored_base_digest": row["base_digest"] if row else base,
    }


def effective(config):
    baseline = validate(config.raw["work_hours"])
    if not config.database_path.is_file():
        return baseline
    conn = None
    try:
        conn = sqlite3.connect(
            config.database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1
        )
        conn.row_factory = sqlite3.Row
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_hours_settings'"
        ).fetchone():
            return baseline
        return snapshot(conn, config)["values"]
    except (sqlite3.Error, ValueError) as exc:
        from .config import ConfigError

        raise ConfigError("工作时间无法核实，暂不执行时间策略") from exc
    finally:
        if conn is not None:
            conn.close()


def history(conn):
    return [
        {
            "revision": row["revision"],
            "previous": validate(json.loads(row["previous_json"])),
            "values": validate(json.loads(row["values_json"])),
            "actor_id": row["actor_id"],
            "updated_at": row["updated_at"],
        }
        for row in conn.execute(
            "SELECT * FROM work_hours_history ORDER BY revision DESC LIMIT 20"
        )
    ]


def preview(
    conn,
    config,
    *,
    values=None,
    expected_revision,
    session_id,
    rollback_revision=None,
    migrate=False,
):
    if type(migrate) is not bool or (migrate and rollback_revision is not None):
        raise ValueError("基础迁移不能与历史回滚混用")
    if rollback_revision is None:
        values = validate(values)
    elif (
        type(rollback_revision) is not int
        or rollback_revision < 1
        or values is not None
    ):
        raise ValueError("回滚必须指定单一历史版本，不能同时提交自定义时段")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("缺少登录会话")
    with transaction(conn):
        current = snapshot(conn, config, allow_mismatch=migrate)
        if migrate and not current["needs_migration"]:
            raise ValueError("基础未变化，无需迁移")
        if rollback_revision is not None:
            historical = conn.execute(
                "SELECT previous_json FROM work_hours_history WHERE revision=?",
                (rollback_revision,),
            ).fetchone()
            if historical is None:
                raise ValueError("工作时间历史版本不存在")
            values = validate(json.loads(historical["previous_json"]))
        if (
            type(expected_revision) is not int
            or expected_revision != current["revision"]
        ):
            raise ValueError("工作时间版本已变化，请重新读取")
        draft = new_id("whd")
        until = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
        conn.execute(
            "INSERT INTO work_hours_drafts(draft_id,session_id,base_digest,revision,values_json,expires_at,source_base_digest) VALUES(?,?,?,?,?,?,?)",
            (
                draft,
                session_id,
                current["base_digest"],
                current["revision"],
                canonical_json(values),
                until,
                current["stored_base_digest"] if migrate else None,
            ),
        )
    return {
        "draft_id": draft,
        "previous": current["values"],
        "proposed": values,
        "timezone": current["timezone"],
        "expires_at": until,
        "rollback_before_revision": rollback_revision,
        "migration": migrate,
        "warning": "旧时区未记录；所选时段按当前时区生效，不重排已有事项"
        if migrate
        else None,
    }


def apply(conn, config, *, draft_id, session_id, actor_id):
    if not isinstance(actor_id, str) or not actor_id:
        raise ValueError("缺少控制者身份")
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM work_hours_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if row is None or row["session_id"] != session_id:
            raise ValueError("工作时间草稿不可用")
        if row["applied_revision"] is not None:
            return {"revision": row["applied_revision"], "replayed": True}
        migrating = row["source_base_digest"] is not None
        current = snapshot(conn, config, allow_mismatch=migrating)
        if (
            current["revision"] != row["revision"]
            or current["base_digest"] != row["base_digest"]
            or parse_iso(row["expires_at"]) <= datetime.now(UTC)
            or (
                migrating
                and (
                    not current["needs_migration"]
                    or current["stored_base_digest"] != row["source_base_digest"]
                )
            )
        ):
            raise ValueError("工作时间草稿已过期或状态变化")
        values = canonical_json(validate(json.loads(row["values_json"])))
        revision, now = current["revision"] + 1, iso_now()
        conn.execute(
            "INSERT INTO work_hours_settings VALUES(1,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,base_digest=excluded.base_digest,values_json=excluded.values_json,actor_id=excluded.actor_id,updated_at=excluded.updated_at",
            (revision, current["base_digest"], values, actor_id, now),
        )
        conn.execute(
            "INSERT INTO work_hours_history VALUES(?,?,?,?,?)",
            (revision, canonical_json(current["values"]), values, actor_id, now),
        )
        conn.execute(
            "UPDATE work_hours_drafts SET applied_revision=? WHERE draft_id=?",
            (revision, draft_id),
        )
        if migrating:
            conn.execute(
                "INSERT INTO work_hours_rebases VALUES(?,?,?,?)",
                (
                    revision,
                    row["source_base_digest"],
                    current["base_digest"],
                    current["timezone"],
                ),
            )
    return {"revision": revision, "replayed": False}
