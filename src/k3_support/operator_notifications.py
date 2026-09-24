"""Explicit routing for ordinary operator notices, separate from public replies."""

from .store import enqueue_outbox

NOTICE_ACTIONS = frozenset({
    "owner_decision", "owner_digest", "approval_request", "post_push_review_failed",
    "wip_push_failed", "review_failed", "incident_alert", "release_impact",
    "board_cleanup_failed", "mail_summary",
})


def destination(config):
    channel = config.raw.get("operator_notifications", {}).get("channel", "telegram")
    if channel == "web":
        return None
    if channel == "feishu":
        chat = config.raw["identity"].get("feishu_control_chat_id")
        return ("feishu_im", chat) if chat else None
    chat = config.telegram_control_chat_id
    return ("telegram", f"telegram:{chat}") if chat else None


def enqueue(conn, config, *, payload, **kwargs):
    if kwargs.get("action_type") not in NOTICE_ACTIONS:
        raise ValueError("unsupported operator notification action")
    target = destination(config)
    if target is None:
        return None, False
    payload = dict(payload)
    if target[0] == "feishu_im":
        # Keep binding metadata for revalidation, but never forward Telegram
        # markup/buttons or private excerpts to a configured Feishu chat.
        payload.pop("buttons", None)
        payload.pop("parse_mode", None)
        case_id = kwargs.get("case_id") or payload.get("case_id")
        label = {
            "incident_alert": "严重问题需要你处理",
            "board_cleanup_failed": "板卡收尾未验证，需要人工处理",
            "wip_push_failed": "代码推送已停止，需要人工处理",
            "post_push_review_failed": "推送后复核未完成，需要人工处理",
            "review_failed": "自动复核已停止，需要人工处理",
            "release_impact": "有新的源码变更影响评估",
        }.get(kwargs["action_type"], "有事项需要你处理")
        payload["text"] = label + ("。" if kwargs["action_type"] == "release_impact" else "。请发送 workbench 查看。")
        approval_id = payload.get("approval_id")
        if approval_id:
            payload["text"] = f"有操作等待审批。请发送 approval {approval_id} 查看完整内容后决定。"
        if case_id:
            payload["text"] += f"\n事项：{case_id}"
    return enqueue_outbox(conn, channel=target[0], destination=target[1],
                          payload=payload, **kwargs)
