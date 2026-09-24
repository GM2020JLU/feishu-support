"""Card 2.0 rendering and native Hermes Feishu callback normalization."""

import asyncio
import json
from types import SimpleNamespace

NAMESPACE = "k3_support_control"


def callback_event(event):
    """Return (owned, normalized); owned malformed events never reach an LLM."""
    text = str(getattr(event, "text", "") or "")
    raw = getattr(getattr(event, "raw_message", None), "event", None)
    value = getattr(getattr(raw, "action", None), "value", None)
    owned = isinstance(value, dict) and NAMESPACE in value
    if not owned:
        # Consume a pasted synthetic callback, but never execute it as a click.
        return text.startswith("/card ") and NAMESPACE in text, None
    source = getattr(event, "source", None)
    context = getattr(raw, "context", None)
    token = getattr(raw, "token", None)
    prompt = getattr(context, "open_message_id", None)
    command = value.get(NAMESPACE)
    if (
        set(value) != {NAMESPACE}
        or not isinstance(command, str)
        or not 1 <= len(command) <= 2048
        or not command.startswith("card-action ")
        or "\n" in command
        or not isinstance(token, str)
        or not token
        or not isinstance(prompt, str)
        or not prompt
        or getattr(context, "open_chat_id", None) != getattr(source, "chat_id", None)
        or not getattr(getattr(raw, "operator", None), "open_id", None)
    ):
        return True, None
    # Hermes currently drops operator.user_id while building synthetic card
    # sources. Prefer the original SDK tenant ID, as ordinary messages do.
    user_id = getattr(getattr(raw, "operator", None), "user_id", None) or getattr(
        source, "user_id", None
    )
    source = SimpleNamespace(
        platform=source.platform, chat_id=source.chat_id, user_id=user_id
    )
    # Audit the stable event token; reply to the actual card, not the token.
    return True, SimpleNamespace(
        text=command,
        source=source,
        message_id=token,
        control_reply_to=prompt,
        raw_message=event.raw_message,
    )


def render(value):
    if value.get("command") not in {
        "feishu_mode",
        "approval_detail",
        "project_bug_approval",
    }:
        return None
    if not value.get("card_id"):
        return None
    choices = []
    for item in value.get("commands", []):
        if "\n" in item:
            label, command = item.split("\n", 1)
        else:
            command = item
            label = (
                "批准"
                if item.startswith(("approve ", "bug approve-close "))
                else "拒绝"
                if item.startswith(("deny ", "bug deny-close "))
                else "工作台"
            )
        choices.append((label, command))
    # Preserve the complete approval text; never offer approval on a truncation.
    text = str(value["text"])
    if len(text) > 12000 or len(choices) > 12:
        return None
    elements = [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]
    for index, (label, command) in enumerate(choices):
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "primary_filled" if index == 0 else "default",
                "width": "fill",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {NAMESPACE: f"card-action {value['card_id']} {index}"},
                    }
                ],
            }
        )
    return {
        "schema": "2.0",
        "config": {"width_mode": "default", "enable_forward": False},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "模式控制"
                if value["command"] == "feishu_mode"
                else "关闭审批"
                if value["command"] == "project_bug_approval"
                else "操作审批",
            },
            "template": "blue",
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "elements": elements,
        },
    }


async def send(adapter, event, value, *, run_control):
    card = render(value)
    sender = getattr(adapter, "_feishu_send_with_retry", None)
    finalizer = getattr(adapter, "_finalize_send_result", None)
    if card is None or not callable(sender) or not callable(finalizer):
        return False
    response = await sender(
        chat_id=str(event.source.chat_id),
        msg_type="interactive",
        payload=json.dumps(card, ensure_ascii=False),
        reply_to=str(getattr(event, "control_reply_to", None) or event.message_id),
        metadata=None,
    )
    result = finalizer(response, "control card send failed")
    if not result.success:
        raise RuntimeError("Feishu control card delivery was not confirmed")
    if not result.message_id:
        raise RuntimeError("Feishu card delivery has no message ID")
    bound, _ = await asyncio.to_thread(
        run_control,
        SimpleNamespace(
            source=event.source,
            message_id=event.message_id,
            text=f"card-bind {value['card_id']} {result.message_id}",
        ),
    )
    if not bound:
        raise RuntimeError("Feishu card delivery binding failed")
    return True
