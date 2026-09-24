"""Chat views and decisions over the existing exact-action closure gate."""

from .project_bug_controls import execute

COMMANDS = {"approvals", "approval", "approve-close", "deny-close"}
STATES = {"requested": "待决定", "approved": "已批准待消费", "denied": "已拒绝",
          "consumed": "已消费", "expired": "已过期", "revoked": "已撤销"}
BLOCKERS = {"approval_not_requested": "审批已经处理", "approval_expired": "审批已过期",
            "verification_changed": "验证证据已变化，需重新申请审批",
            "verification_unavailable": "当前修复或验证证据不可用，请先检查复核状态"}


def plain(value):
    # Remote identifiers remain data, even if they contain line breaks.
    return ' '.join(str(value).split())


def route(conn, config, argv, request_id):
    from .control import ControlError

    name = argv[1]
    if name == "approvals" and len(argv) == 3:
        result = execute(conn, config, action="close-approvals", payload={"bug_id": argv[2]})
        lines = ["关闭审批（最近 10 项）"]
        for item in result["items"]:
            lines.append(f"{STATES.get(item['status'], item['status'])} · 到期 {item['expires_at']}\n"
                         f"查看完整动作：bug approval {item['approval_id']}")
        if not result["items"]:
            lines.append("暂无关闭审批。请先在网页核验验证证据并发起具体关闭申请。")
        return {"command": "project_bug", "text": '\n\n'.join(lines)}
    if name == "approval" and len(argv) == 3:
        value = execute(conn, config, action="close-approval-detail", payload={"approval_id": argv[2]})
        intent = value["action"]
        current = value["current_verification"]
        lines = ["飞书项目 Bug 关闭审批", f"审批：{value['approval_id']}",
                 f"状态：{STATES.get(value['status'], value['status'])}" + ("（已过有效期）" if value['expired'] else ''),
                 f"Bug：{value['bug_id']}",
                 '远端对象：' + ' / '.join(plain(intent[k]) for k in ('host', 'project_key', 'type_key', 'item_id')),
                 f"关闭流转：{plain(intent['transition_id'])}", f"目标状态：{plain(intent['target_status_id'])}",
                 f"有效至：{value['expires_at']}",
                 f"申请时验证摘要：{value['verification_digest']}"]
        if value["status"] == "consumed":
            lines.append("审批已用于一次关闭写入；关闭是否生效以远端状态和写入回执为准。")
        else:
            lines.extend([f"当前验证：{current['verification_state'] if current else '不可用'}",
                          "批准允许匹配且已授权的关闭写入消费此审批；审批本身不是远端关闭成功回执。"])
        if value["status"] != "consumed" and value["approval_blockers"]:
            lines.append("当前限制：" + '；'.join(BLOCKERS[k] for k in value["approval_blockers"]))
        commands = []
        if value["can_approve"]:
            commands.append(f"批准此关闭审批\nbug approve-close {value['approval_id']} {value['action_digest']}")
        if value["can_deny"]:
            commands.append(f"拒绝此关闭审批\nbug deny-close {value['approval_id']} {value['action_digest']}")
        return {"command": "project_bug_approval", "text": '\n'.join(lines), "commands": commands}
    if name in {"approve-close", "deny-close"} and len(argv) == 4:
        result = execute(conn, config, action="decide-close-approval", payload={
            "approval_id": argv[2], "expected_digest": argv[3], "request_id": request_id,
            "approve": name == "approve-close"})
        return {"command": "project_bug", "text":
                f"审批状态：{STATES.get(result['status'], result['status'])}\n"
                f"查看审批：bug approval {result['approval_id']}\n查看 Bug：bug {result['bug_id']}\n"
                "本命令只记录审批决定，不另建关闭写入；最终关闭结果须以远端回读为准。"}
    raise ControlError("用法：bug approvals <Bug ID> / bug approval <审批 ID> / bug approve-close|deny-close <审批 ID> <动作摘要>")
