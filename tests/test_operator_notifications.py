import json

from k3_support.operator_notifications import destination, enqueue
from k3_support.store import create_case


def test_feishu_notice_omits_private_content_and_telegram_actions(conn, config):
    config.raw["operator_notifications"] = {"channel": "feishu"}
    config.raw["identity"]["feishu_control_chat_id"] = "control-chat"
    case, _ = create_case(conn, title="private title", case_type="bug",
                          severity="P2", confidence=1)
    identifier, created = enqueue(
        conn, config, action_type="owner_decision", case_id=case,
        idempotency_key="private-notice", payload={
            "text": "private evidence", "parse_mode": "HTML",
            "buttons": [{"callback_data": "telegram-only"}],
            "control_turn_id": "bound-turn", "control_fence": 2,
        },
    )
    row = conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (identifier,)).fetchone()
    payload = json.loads(row["payload_json"])
    assert created and row["channel"] == "feishu_im"
    assert row["destination"] == "control-chat"
    assert "private" not in payload["text"] and case in payload["text"]
    assert "buttons" not in payload and "parse_mode" not in payload
    assert payload["control_turn_id"] == "bound-turn" and payload["control_fence"] == 2


def test_web_notifications_do_not_enqueue_external_delivery(conn, config):
    config.raw["operator_notifications"] = {"channel": "web"}
    before = conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
    assert enqueue(conn, config, action_type="owner_decision",
                   idempotency_key="web-only", payload={"text": "private"}) == (None, False)
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == before


def test_default_destination_preserves_telegram(config):
    config.raw.pop("operator_notifications", None)
    assert destination(config) == ("telegram", f"telegram:{config.telegram_control_chat_id}")


def test_approval_notice_requires_viewing_details_not_direct_callback(conn, config):
    config.raw["operator_notifications"] = {"channel": "feishu"}
    config.raw["identity"]["feishu_control_chat_id"] = "control-chat"
    identifier, _ = enqueue(
        conn, config, action_type="approval_request", idempotency_key="approval-notice",
        payload={"approval_id": "apr-example", "text": "private command",
                 "buttons": [{"callback_data": "k3a:a:apr-example"}]},
    )
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM outbox WHERE outbox_id=?", (identifier,)
    ).fetchone()[0])
    assert "approval apr-example" in payload["text"]
    assert "private command" not in payload["text"]
    assert "buttons" not in payload


def test_public_reply_cannot_enter_operator_route(conn, config):
    import pytest

    with pytest.raises(ValueError, match="unsupported"):
        enqueue(conn, config, action_type="reply", payload={"text": "external"},
                idempotency_key="public-reply")
