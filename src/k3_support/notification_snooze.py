"""Operator-requested deferral of ordinary decision notices, not execution.

No task/approval/ownership mutation, and no external calls. A pause cannot
silence P0/P1, incident alerts, board approvals, or outgoing colleague replies.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from .db import transaction
from .ids import digest


def _now(now):
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("notification time requires a timezone")
    return value.astimezone(UTC)


def status(conn, *, now=None, config=None):
    observed = _now(now)
    row = conn.execute("SELECT * FROM notification_snooze WHERE singleton=1").fetchone()
    until = row["until_at"] if row else None
    manual_active = until is not None and datetime.fromisoformat(until) > observed
    enabled = bool(row and row["night_enabled"])
    night = None
    if enabled and config is not None:
        from .notification_schedule import window

        night = window(config, observed)
    night_active = enabled and (night is None or night["active"])
    deadlines = [
        value
        for value in (
            until if manual_active else None,
            night["until_at"] if night else None,
        )
        if value
    ]
    return {
        "revision": row["revision"] if row else 0,
        "until_at": max(deadlines) if deadlines else until,
        "manual_until_at": until,
        "manual_active": manual_active,
        "night_enabled": enabled,
        "night_active": night_active,
        "night_cutoff": night["cutoff"] if night else None,
        "active": manual_active or night_active,
        "read_only": True,
        "scope": "ordinary_owner_decision_notices",
        "urgent_notifications_affected": False,
        "tasks_or_approvals_changed": False,
    }


def set_snooze(
    conn,
    *,
    minutes,
    expected_revision,
    request_id,
    actor_id,
    now=None,
    night_enabled=None,
):
    """Call only after authenticating an operator; no model-provided identity."""
    if (
        not actor_id
        or (minutes is None and night_enabled is None)
        or (
            minutes is not None
            and (type(minutes) is not int or not 0 <= minutes <= 1440)
        )
    ):
        raise ValueError("需要操作员身份和 0–1440 分钟；0 表示恢复普通提醒")
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("需要稳定的 UUID 请求号")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("需要当前提醒设置版本")
    if night_enabled is not None and type(night_enabled) is not bool:
        raise ValueError("夜间汇总开关必须为布尔值")
    request = {
        "actor": actor_id,
        "minutes": minutes,
        "expected_revision": expected_revision,
    }
    if night_enabled is not None:
        request["night_enabled"] = night_enabled
    fingerprint = digest(request)
    observed = _now(now)
    with transaction(conn):
        previous = conn.execute(
            "SELECT * FROM notification_snooze_history WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if previous:
            if previous["request_digest"] != fingerprint:
                raise ValueError("同一请求号不能改变免打扰设置")
            return {
                "revision": previous["revision"],
                "until_at": previous["until_at"],
                "replayed": True,
                "night_enabled": bool(previous["night_enabled"]),
            }
        current = status(conn, now=observed)
        if current["revision"] != expected_revision:
            raise ValueError("提醒设置已变化，请重新读取")
        revision = current["revision"] + 1
        until = (
            current["manual_until_at"]
            if minutes is None
            else (
                (observed + timedelta(minutes=minutes)).isoformat() if minutes else None
            )
        )
        night_enabled = (
            current["night_enabled"] if night_enabled is None else night_enabled
        )
        stamp = observed.isoformat()
        conn.execute(
            "INSERT INTO notification_snooze VALUES(1,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,until_at=excluded.until_at,updated_by=excluded.updated_by,updated_at=excluded.updated_at,night_enabled=excluded.night_enabled",
            (revision, until, actor_id, stamp, night_enabled),
        )
        conn.execute(
            "INSERT INTO notification_snooze_history VALUES(?,?,?,?,?,?,?)",
            (request_id, fingerprint, revision, until, actor_id, stamp, night_enabled),
        )
    return {
        "revision": revision,
        "until_at": until,
        "replayed": False,
        "night_enabled": night_enabled,
    }


def deferred(conn, row, *, now=None, config=None):
    if row.get("channel") not in {"telegram", "feishu_im"} or row.get("action_type") != "owner_decision":
        return False
    case = conn.execute(
        "SELECT severity FROM cases WHERE case_id=?", (row.get("case_id"),)
    ).fetchone()
    # Unknown severity must not be interpreted as an ordinary issue. Never
    # accept a payload-provided severity in place of the authoritative Case.
    if case is None or case["severity"] not in {"P2", "P3"}:
        return False
    if status(conn, now=now, config=config)["active"]:
        return True
    from .notification_digest import cutoff

    boundary = cutoff(conn, config=config, now=now)
    return bool(boundary and row.get("created_at") and row["created_at"] <= boundary)
