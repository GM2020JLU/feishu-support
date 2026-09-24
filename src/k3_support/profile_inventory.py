"""Read-only cached colleague profiles; never refreshes directory data."""

from datetime import UTC, datetime

from .ids import digest
from .routing import _profile_dict, audience_strategy, effective_requester_profile
from .timeutil import parse_iso


def page(conn, *, query="", after_id="", limit=30):
    if (
        not isinstance(query, str)
        or len(query) > 256
        or not isinstance(after_id, str)
        or len(after_id) > 512
        or type(limit) is not int
        or not 1 <= limit <= 50
    ):
        raise ValueError("无效的角色资料筛选")
    rows = conn.execute(
        """SELECT * FROM requester_profiles WHERE requester_id>?
        AND (?='' OR instr(lower(requester_id||' '||coalesce(display_name,'')||' '||coalesce(department,'')),lower(?))>0)
        ORDER BY requester_id LIMIT ?""",
        (after_id, query, query, limit + 1),
    ).fetchall()
    now = datetime.now(UTC)
    items = []
    for row in rows[:limit]:
        profile = _profile_dict(row)
        effective = effective_requester_profile(row, now=now)
        expiry = "not_recorded"
        if row["expires_at"]:
            try:
                expiry = (
                    "expired" if parse_iso(row["expires_at"]) <= now else "not_expired"
                )
            except (TypeError, ValueError):
                expiry = "invalid"
        items.append(
            {
                **profile,
                "content_digest": digest(dict(row)),
                "expires_at": row["expires_at"],
                "expiry": expiry,
                "cached_strategy": audience_strategy(profile),
                "effective_profile": effective,
                "effective_strategy": audience_strategy(effective),
                "authority_status": (
                    "operator_override"
                    if row["source"] == "operator"
                    else "untrusted"
                    if effective["source"] == "unknown"
                    else "valid_cache"
                ),
            }
        )
    return {
        "items": items,
        "next_after_id": rows[limit - 1]["requester_id"] if len(rows) > limit else None,
        "read_only": True,
        "directory_refreshed": False,
        "scope": "展示原始缓存及按当前有效期计算的角色策略；非最终消息路由，未查询通讯录，人工覆盖持续生效直到撤销",
    }
