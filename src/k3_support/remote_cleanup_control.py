"""Telegram identity/prompt-bound recovery; natural-language text is not approval."""

import re
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .approvals import verify_control_identity
from .broker_process import run_process
from .broker_remote_cleanup import apply, preview
from .broker_remote_observation import record
from .db import transaction


def route(conn, config, message, *, prompt_message_id, callback_data, transport=run_process):
    verify_control_identity(config, message.user_id, message.chat_id)
    match = re.fullmatch(r"rr:([qac]):([0-9a-f]{32})", callback_data)
    if not match or not isinstance(prompt_message_id, str) or not prompt_message_id:
        raise ValueError("invalid remote recovery callback")
    operation, coordinate = match.groups()
    buttons = []
    if operation == "q":
        request_id = str(UUID(hex=coordinate))
        observation_id = str(uuid5(NAMESPACE_URL, repr((message.user_id, message.chat_id,
                                                       prompt_message_id, message.message_id, request_id))))
        checked = record(conn, config, request_id=request_id, observation_id=observation_id, transport=transport)
        if checked["state"] != "observed":
            text = f"远端核对仍未确认（{checked['state']}）。\n观察编号：{observation_id}\n不会重跑或强行解除占用。"
        else:
            shown = preview(conn, config, request_id=request_id, observation_id=observation_id)
            token = uuid4().hex
            with transaction(conn):
                conn.execute("INSERT INTO broker_remote_cleanup_panels VALUES(?,?,?,?,?,?,?,?,0)",
                             (token, request_id, observation_id, shown["preview_digest"], message.user_id,
                              message.chat_id, prompt_message_id, (datetime.now(UTC)+timedelta(minutes=5)).isoformat()))
            text = f"解除远端占用？\n请求：{request_id}\n" + shown["summary"]
            buttons = [{"text": "确认解除占用", "callback_data": "rr:a:"+token, "row": 0},
                       {"text": "取消", "callback_data": "rr:c:"+token, "row": 0}]
    else:
        panel = conn.execute("SELECT * FROM broker_remote_cleanup_panels WHERE token=?", (coordinate,)).fetchone()
        if (not panel or (panel["user_id"], panel["chat_id"], panel["prompt_message_id"]) !=
                (message.user_id, message.chat_id, prompt_message_id) or panel["cancelled"]):
            raise ValueError("remote recovery panel identity changed")
        expiry = datetime.fromisoformat(panel["expires_at"])
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise ValueError("remote recovery confirmation expired")
        if operation == "c":
            with transaction(conn):
                applied = conn.execute("SELECT 1 FROM broker_remote_cleanup WHERE request_id=?", (panel["request_id"],)).fetchone()
                if not applied:
                    conn.execute("UPDATE broker_remote_cleanup_panels SET cancelled=1 WHERE token=?", (coordinate,))
            text = "确认已生效；取消不会撤销已记录的清理。" if applied else "已取消；远端占用未改变。"
        else:
            apply(conn, config, request_id=panel["request_id"], observation_id=panel["observation_id"],
                  preview_digest=panel["preview_digest"], panel_token=coordinate,
                  panel_identity=(message.user_id, message.chat_id, prompt_message_id))
            text = "已记录远端清理确认。命令结果仍未知；未重跑、未确认修复、未发送回复。"
    return {"command": "remote_recovery", "preview": {"text": text, "buttons": buttons},
            "display_target": {"kind": "edit_original", "chat_id": message.chat_id, "message_id": prompt_message_id}}
