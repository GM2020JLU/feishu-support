import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_broker_remote_cleanup import (
    cleanup_candidate as cleanup_candidate,  # noqa: PLC0414 - pytest fixture re-export
)
from test_broker_remote_cleanup import (
    unresolved as unresolved,  # noqa: PLC0414 - pytest fixture re-export
)
from test_broker_remote_probe import journal_remote  # noqa: F401
from test_broker_remote_runner import remote  # noqa: F401

from k3_support.approvals import ApprovalError
from k3_support.remote_cleanup_control import route


def open_panel(conn, cleanup_candidate):
    cfg, _, ids = cleanup_candidate
    message = SimpleNamespace(user_id=cfg.telegram_control_user_id, chat_id=cfg.telegram_control_chat_id, message_id="query-1")
    plan = json.loads(conn.execute("SELECT plan_json FROM broker_remote_actions").fetchone()[0])
    def transport(**kwargs):
        value = {"state": "guardian_returned", "version": 1, "request_id": ids["request_id"],
                 "command_digest": plan["command_digest"], "guard_exit_code": 124}
        return {"exit_code": 0, "stdout": json.dumps(value, sort_keys=True)+"\n", "stderr": ""}
    panel = route(conn, cfg, message, prompt_message_id="42", callback_data="rr:q:"+UUID(ids["request_id"]).hex, transport=transport)
    return cfg, message, panel


def test_phone_queries_then_requires_bound_confirmation(conn, cleanup_candidate):
    cfg, message, panel = open_panel(conn, cleanup_candidate)
    assert unresolved(conn)
    buttons = panel["preview"]["buttons"]
    assert len(buttons) == 2 and all(len(b["callback_data"].encode()) <= 64 for b in buttons)
    with pytest.raises(ValueError, match="identity"):
        route(conn, cfg, message, prompt_message_id="unrelated", callback_data=buttons[0]["callback_data"])
    assert unresolved(conn)
    result = route(conn, cfg, message, prompt_message_id="42", callback_data=buttons[0]["callback_data"])
    assert not unresolved(conn) and result["preview"]["buttons"] == []
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_cancelled_phone_confirmation_cannot_apply(conn, cleanup_candidate):
    cfg, message, panel = open_panel(conn, cleanup_candidate)
    confirm, cancel = panel["preview"]["buttons"]
    route(conn, cfg, message, prompt_message_id="42", callback_data=cancel["callback_data"])
    with pytest.raises(ValueError, match="identity"):
        route(conn, cfg, message, prompt_message_id="42", callback_data=confirm["callback_data"])
    assert unresolved(conn)


def test_phone_rejects_wrong_owner_and_expired_confirmation(conn, cleanup_candidate):
    cfg, message, panel = open_panel(conn, cleanup_candidate)
    callback = panel["preview"]["buttons"][0]["callback_data"]
    wrong = SimpleNamespace(user_id="wrong", chat_id=message.chat_id, message_id="other")
    with pytest.raises(ApprovalError):
        route(conn, cfg, wrong, prompt_message_id="42", callback_data=callback)
    conn.execute("UPDATE broker_remote_cleanup_panels SET expires_at='2000-01-01T00:00:00+00:00'")
    with pytest.raises(ValueError, match="expired"):
        route(conn, cfg, message, prompt_message_id="42", callback_data=callback)
    assert unresolved(conn)
