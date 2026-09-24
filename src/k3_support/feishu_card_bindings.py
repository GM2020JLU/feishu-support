"""Durable authority for Feishu cards, issued only from control-plane results."""

import json
from datetime import timedelta

from .db import transaction
from .ids import new_id
from .timeutil import iso_now, parse_iso, utc_now


def issue(conn, message, result):
    if result.get("command") not in {
        "feishu_mode",
        "approval_detail",
        "project_bug_approval",
    }:
        return result
    commands = [item.split("\n", 1)[-1] for item in result["commands"]]
    card_id = new_id("fcc")
    with transaction(conn):
        conn.execute(
            """INSERT INTO feishu_control_cards
            (card_id,user_id,chat_id,request_message_id,commands_json,expires_at,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (
                card_id,
                message.user_id,
                message.chat_id,
                message.message_id,
                json.dumps(commands),
                (utc_now() + timedelta(minutes=30)).isoformat(),
                iso_now(),
            ),
        )
    return {**result, "card_id": card_id}


def _owned(conn, message, card_id):
    from .control import ControlError

    row = conn.execute(
        "SELECT * FROM feishu_control_cards WHERE card_id=?", (card_id,)
    ).fetchone()
    if (
        row is None
        or row["user_id"] != message.user_id
        or row["chat_id"] != message.chat_id
        or parse_iso(row["expires_at"]) <= utc_now()
    ):
        raise ControlError("Feishu card identity or expiry mismatch")
    return row


def bind(conn, message, card_id, delivered_message_id):
    from .control import ControlError

    if not delivered_message_id.startswith("om_") or len(delivered_message_id) > 256:
        raise ControlError("invalid Feishu card message ID")
    with transaction(conn):
        row = _owned(conn, message, card_id)
        if row["request_message_id"] != message.message_id or row[
            "delivered_message_id"
        ] not in {None, delivered_message_id}:
            raise ControlError("Feishu card delivery binding mismatch")
        conn.execute(
            "UPDATE feishu_control_cards SET delivered_message_id=? WHERE card_id=?",
            (delivered_message_id, card_id),
        )
    return {"command": "card_bound", "card_id": card_id}


def resolve(conn, message, card_id, index):
    from .control import ControlError

    row = _owned(conn, message, card_id)
    if (
        not message.source_card_message_id
        or row["delivered_message_id"] != message.source_card_message_id
    ):
        raise ControlError("Feishu card is not bound to this message")
    commands = json.loads(row["commands_json"])
    if not index.isdecimal() or len(index) > 2 or not 0 <= int(index) < len(commands):
        raise ControlError("invalid Feishu card action")
    return commands[int(index)]
