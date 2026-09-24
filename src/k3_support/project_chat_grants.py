"""Narrow Bug read/comment/field grants through authenticated chat controls."""

import json
from datetime import timedelta

from .project_bug_controls import execute
from .project_bug_grants import projection
from .project_bugs import detail
from .timeutil import utc_now

COMMANDS = {"grant-options", "grants", "grant", "revoke-grant"}
GRANT_USAGE = "bug grant <Bug ID> <1|8|24 小时> '{\"comment\":true,\"fields\":[\"字段 ID\"]}'"
USAGE = ("bug grant-options <Bug ID> [字段游标] | bug grants <Bug ID> [授权游标] | "
         + GRANT_USAGE + " | bug revoke-grant <授权 ID>")


def _selection(raw, observed):
    try:
        selected = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("授权选项必须是 JSON 对象") from exc
    if not isinstance(selected, dict) or set(selected) != {"comment", "fields"}:
        raise ValueError("授权选项仅支持 comment 和 fields")
    fields = selected["fields"]
    if (type(selected["comment"]) is not bool or not isinstance(fields, list)
            or len(fields) > 200 or any(not isinstance(key, str) for key in fields)
            or len(fields) != len(set(fields))):
        raise ValueError("授权字段列表或评论选项无效")
    if not set(fields) <= observed:
        raise ValueError("只能授权当前快照已观测的字段；请刷新 Bug 后再核对")
    return selected


def route(conn, config, argv, request_id):
    from .control import ControlError

    name = argv[1]
    if name == "grant-options" and len(argv) in {3, 4}:
        bug = detail(conn, argv[2])
        fields = sorted((bug["snapshot"] or {}).get("fields", {}))
        after = argv[3] if len(argv) == 4 else ""
        page = [key for key in fields if key > after][:30]
        lines = [f"当前 Bug：{bug['bug_id']} · {bug['project_key']} / {bug['type_key']}",
                 "聊天授权仅支持读取、评论及已观测字段；授权不会立即写入飞书项目。",
                 "已观测字段：" + ("、".join(page) if page else "暂无")]
        if len([key for key in fields if key > after]) > len(page):
            lines.append(f"下一页：bug grant-options {bug['bug_id']} {page[-1]}")
        lines.append("签发：" + GRANT_USAGE)
        return {"command": "project_bug", "text": "\n".join(lines)}
    if name == "grants" and len(argv) in {3, 4}:
        bug = detail(conn, argv[2])
        result = execute(conn, config, action="list-grants", payload={
            "bug_id": bug["bug_id"], "after_id": argv[3] if len(argv) == 4 else "",
        })
        lines = ["当前 Bug 的授权（只列当前操作员）"]
        for item in result["items"]:
            scope = item["scope"]
            lines.append(f"{item['grant_id']} · {item['status']} · 到期 {item['expires_at']} · "
                         f"动作 {','.join(scope['actions'])} · 字段 {','.join(scope['fields']) or '无'}")
        if not result["items"]:
            lines.append("暂无授权")
        if result["next_cursor"]:
            lines.append(f"下一页：bug grants {bug['bug_id']} {result['next_cursor']}")
        return {"command": "project_bug", "text": "\n".join(lines)}
    if name == "grant" and len(argv) == 5:
        if argv[3] not in {"1", "8", "24"}:
            raise ValueError("授权有效期只能是 1、8 或 24 小时")
        old = conn.execute(
            "SELECT * FROM project_bug_grants WHERE actor=? AND request_id=?",
            (config.control_operator_id, request_id),
        ).fetchone()
        if old is not None:
            result = projection(old)
        else:
            bug = detail(conn, argv[2])
            observed = set((bug["snapshot"] or {}).get("fields", {}))
            selected = _selection(argv[4], observed)
            fields = selected["fields"]
            scope = {"host": bug["host"], "project_key": bug["project_key"],
                     "type_key": bug["type_key"], "bug_ids": [bug["bug_id"]],
                     "actions": ["bug.read", *(["bug.comment"] if selected["comment"] else []),
                                 *(["bug.fields"] if fields else [])],
                     "fields": fields, "transitions": [], "repositories": [], "devices": []}
            result = execute(conn, config, action="issue-grant", payload={
                "request_id": request_id, "scope": scope,
                "expires_at": (utc_now() + timedelta(hours=int(argv[3]))).isoformat(),
            })
        return {"command": "project_bug", "text":
                f"授权：{result['grant_id']} · {result['status']} · 到期 {result['expires_at']}\n"
                f"范围：{result['scope']['project_key']} / {result['scope']['type_key']} / "
                f"{','.join(result['scope']['bug_ids'])}\n"
                "授权不会立即执行；远端权限、运行模式和写入门槛仍须核验。"}
    if name == "revoke-grant" and len(argv) == 3:
        result = execute(conn, config, action="revoke-grant", payload={"grant_id": argv[2]})
        return {"command": "project_bug", "text":
                f"授权 {result['grant_id']} 已撤销；在途操作仍需按原对象核对结果。"}
    raise ControlError(USAGE)
