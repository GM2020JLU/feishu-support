# ruff: noqa: F811 -- shared isolated fixtures
"""Local-only chat write controls; dispatch workers and Project are never run."""

import copy
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_project_bug_operations import setup  # noqa: F401
from test_project_comment_dispatch import ready as ready_comment
from test_project_write_dispatch import ready as ready_write

from k3_support import (
    project_activity,
    project_bug_controls,
    project_bugs,
    project_write_dispatch,
)
from k3_support import project_bug_grants as grants
from k3_support import project_bug_operations as operations
from k3_support import project_chat_writes as chat_writes
from k3_support.control import ControlError
from k3_support.project_bugs import BugConflict
from k3_support.timeutil import utc_now


def _operation(conn, setup, *, action="bug.fields", change=None, actor="owner", request_id="chat-test"):
    cfg, request, _adapter = setup
    return cfg, operations.prepare(
        conn,
        **(request | {
            "actor": actor,
            "request_id": request_id,
            "action": action,
            "change": change or {"fields": {"progress": "new"}},
        }),
    )


def _route(conn, cfg, argv, request_id="native-message-1"):
    return chat_writes.route(conn, cfg, ["bug", *argv], request_id)


def test_full_preview_is_owned_exact_and_uses_existing_send_queue(conn, setup, monkeypatch):
    cfg, op, _args, _native = ready_comment(conn, setup, monkeypatch)
    preview = _route(conn, cfg, ["write", op["operation_id"]])
    assert "Reviewed conclusion" in preview["text"]
    assert f"bug send-write {op['operation_id']} {op['request_digest']}" in preview["text"]
    assert f"{op['bug_id']}" in preview["text"]
    assert conn.execute("SELECT count(*) FROM project_bug_events WHERE kind='chat_write_intent'").fetchone()[0] == 0

    stranger = copy.deepcopy(cfg)
    stranger.raw["identity"]["control_operator_id"] = "stranger"
    with pytest.raises(ValueError, match="owned"):
        _route(conn, stranger, ["write", op["operation_id"]], "another-user")


def test_oversize_preview_has_no_executable_command_and_manual_send_is_rejected(conn, setup):
    cfg, op = _operation(conn, setup, action="bug.comment", change={"text": "x" * 3200})
    preview = _route(conn, cfg, ["write", op["operation_id"]])
    assert "超过聊天显示上限" in preview["text"]
    assert "bug send-write" not in preview["text"]
    with pytest.raises(ControlError, match="超过聊天显示上限"):
        _route(conn, cfg, ["send-write", op["operation_id"], op["request_digest"]], "oversize-send")
    assert conn.execute("SELECT count(*) FROM project_bug_events WHERE kind='chat_write_intent'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM project_comment_dispatch_requests").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM project_write_dispatch_requests").fetchone()[0] == 0


def test_changed_digest_and_revoked_grant_do_not_enter_dispatch_queue(conn, setup, monkeypatch):
    cfg, op, _args, _native = ready_write(conn, setup, monkeypatch)
    with pytest.raises(ControlError, match="摘要不匹配"):
        _route(conn, cfg, ["send-write", op["operation_id"], "wrong-digest"], "bad-digest")
    grants.revoke(conn, actor="owner", grant_id=op["grant_id"])
    preview = _route(conn, cfg, ["write", op["operation_id"]])
    assert "authority_or_source_changed" in preview["text"]
    with pytest.raises(ControlError, match="authority_or_source_changed"):
        _route(conn, cfg, ["send-write", op["operation_id"], op["request_digest"]], "revoked-send")
    assert conn.execute("SELECT count(*) FROM project_write_dispatch_requests").fetchone()[0] == 0


def test_native_message_is_bound_across_field_comment_queues_and_exact_retry(conn, setup, monkeypatch):
    cfg, op, _args, _native = ready_write(conn, setup, monkeypatch)
    argv = ["send-write", op["operation_id"], op["request_digest"]]
    first = _route(conn, cfg, argv, "same-native-message")
    assert "已进入现有写入队列" in first["text"]
    intent_count = conn.execute("SELECT count(*) FROM project_bug_events WHERE kind='chat_write_intent'").fetchone()[0]
    assert intent_count == 1
    second = _route(conn, cfg, argv, "same-native-message")
    assert "已有写入队列回执" in second["text"]
    assert conn.execute("SELECT count(*) FROM project_bug_events WHERE kind='chat_write_intent'").fetchone()[0] == 1
    with pytest.raises(ControlError, match="正在处理"):
        _route(conn, cfg, argv, "different-native-message")
    with pytest.raises(BugConflict, match="different write intent"):
        _route(conn, cfg, ["cancel-write", op["operation_id"], op["request_digest"]], "same-native-message")
    operations.cancel(conn, operation_id=op["operation_id"], actor="owner", expected_digest=op["request_digest"])
    comment = operations.prepare(
        conn, bug_id=op["bug_id"], snapshot_id=op["snapshot_id"], actor="owner",
        request_id="comment-after-write", grant_id=op["grant_id"],
        expected_revision=project_bugs.detail(conn, op["bug_id"])["revision"],
        action="bug.comment", change={"text": "must not cross queue identity"},
    )
    with pytest.raises(BugConflict, match="different write intent"):
        _route(conn, cfg, ["send-write", comment["operation_id"], comment["request_digest"]], "same-native-message")
    assert conn.execute("SELECT count(*) FROM project_comment_dispatch_requests").fetchone()[0] == 0


def test_cancel_command_replays_the_same_local_cancellation(conn, setup):
    cfg, op = _operation(conn, setup)
    argv = ["cancel-write", op["operation_id"], op["request_digest"]]
    first = _route(conn, cfg, argv, "same-cancel-message")
    second = _route(conn, cfg, argv, "same-cancel-message")
    assert "cancelled" in first["text"] and "cancelled" in second["text"]
    assert conn.execute("SELECT count(*) FROM project_bug_events WHERE kind='chat_write_intent'").fetchone()[0] == 1
    with pytest.raises(BugConflict, match="different write intent"):
        _route(conn, cfg, ["send-write", op["operation_id"], op["request_digest"]], "same-cancel-message")


def test_terminal_send_receipt_replays_without_reentering_dispatch(conn, setup, monkeypatch):
    cfg, op, _args, _native = ready_write(conn, setup, monkeypatch)
    argv = ["send-write", op["operation_id"], op["request_digest"]]
    first = _route(conn, cfg, argv, "terminal-send-message")
    assert "已进入现有写入队列" in first["text"]
    _cfg, _request, adapter = setup
    stored = conn.execute(
        "SELECT payload_json FROM project_bug_snapshots WHERE snapshot_id=?", (op["snapshot_id"],)
    ).fetchone()
    adapter.view = replace(adapter.view, snapshot=json.loads(stored["payload_json"]))
    adapter.timeout_after_apply = True
    adapter.result = "unknown"
    dispatched = operations.dispatch(conn, cfg, operation_id=op["operation_id"], transport=adapter)
    assert dispatched["state"] == "unknown"
    row = conn.execute("SELECT * FROM project_write_dispatch_requests WHERE operation_id=?", (op["operation_id"],)).fetchone()
    project_write_dispatch._finish(conn, row, "succeeded", result={"operation_state": "unknown", "reconcile_required": True})
    replay = _route(conn, cfg, argv, "terminal-send-message")
    assert "已有写入队列回执" in replay["text"] and "succeeded" in replay["text"]
    assert len(adapter.writes) == 1
    assert conn.execute("SELECT count(*) FROM project_write_dispatch_requests WHERE operation_id=?", (op["operation_id"],)).fetchone()[0] == 1


def test_unknown_operation_shows_reconcile_only_and_refuses_resend(conn, setup):
    cfg, op = _operation(conn, setup)
    _cfg, _request, adapter = setup
    adapter.timeout_after_apply = True
    adapter.result = "unknown"
    result = operations.dispatch(conn, cfg, operation_id=op["operation_id"], transport=adapter)
    assert result["state"] == "unknown"
    preview = _route(conn, cfg, ["write", op["operation_id"]])
    assert "bug reconcile-write" in preview["text"]
    assert "bug send-write" not in preview["text"]
    with pytest.raises(ControlError, match="不会重复发送"):
        _route(conn, cfg, ["send-write", op["operation_id"], op["request_digest"]], "unknown-resend")
    assert len(adapter.writes) == 1
    assert conn.execute("SELECT count(*) FROM project_write_dispatch_requests").fetchone()[0] == 0


def test_reconcile_replay_returns_existing_read_only_receipt_after_terminal_settlement(conn, setup, monkeypatch):
    cfg, op, _args, _native = ready_write(conn, setup, monkeypatch)
    _cfg, _request, adapter = setup
    stored = conn.execute(
        "SELECT payload_json FROM project_bug_snapshots WHERE snapshot_id=?", (op["snapshot_id"],)
    ).fetchone()
    adapter.view = replace(adapter.view, snapshot=json.loads(stored["payload_json"]))
    adapter.timeout_after_apply = True
    adapter.result = "unknown"
    dispatched = operations.dispatch(conn, cfg, operation_id=op["operation_id"], transport=adapter)
    assert dispatched["state"] == "unknown" and dispatched["write_digest"]
    read_grant = grants.issue(
        conn, actor="owner", request_id="reconcile-read-grant",
        scope={
            "host": "project.feishu.cn", "project_key": "space", "type_key": "type",
            "bug_ids": [op["bug_id"]], "actions": ["bug.read"], "fields": [],
            "transitions": [], "repositories": [], "devices": [],
        },
        expires_at=(utc_now() + timedelta(hours=1)).isoformat(),
    )
    argv = ["reconcile-write", op["operation_id"], dispatched["write_digest"], read_grant["grant_id"]]
    first = _route(conn, cfg, argv, "same-reconcile-message")
    assert "只读核对已进入现有队列" in first["text"]
    row = conn.execute(
        "SELECT * FROM project_activity_requests WHERE actor='owner' AND request_id='same-reconcile-message'"
    ).fetchone()
    assert row["kind"] == "write_reconcile" and row["state"] == "queued"
    project_activity._finish(conn, row, "succeeded", result={"test_local_read_only_receipt": True})
    project_bug_controls.execute(conn, cfg, action="settle-unknown-write", payload={
        "operation_id": op["operation_id"], "verdict": "confirmed_not_applied",
        "evidence_text": "synthetic isolated test settlement",
    })
    assert operations._operation(conn, op["operation_id"])["state"] == "rejected"
    replay = _route(conn, cfg, argv, "same-reconcile-message")
    assert "已有只读核对回执" in replay["text"] and "succeeded" in replay["text"]
    assert conn.execute(
        "SELECT count(*) FROM project_activity_requests WHERE actor='owner' AND request_id='same-reconcile-message'"
    ).fetchone()[0] == 1
    assert len(adapter.writes) == 1


def test_list_is_limited_to_actor_local_operations(conn, setup):
    cfg, op = _operation(conn, setup)
    listed = _route(conn, cfg, ["writes", op["bug_id"]])
    assert op["operation_id"] in listed["text"]
    stranger = copy.deepcopy(cfg)
    stranger.raw["identity"]["control_operator_id"] = "stranger"
    private = _route(conn, stranger, ["writes", op["bug_id"]])
    assert op["operation_id"] not in private["text"]
    assert "没有当前操作人的写入记录" in private["text"]


def test_close_preview_and_chat_send_keep_existing_approval_gate(conn, setup, monkeypatch):
    cfg, op, _args, _native = ready_write(
        conn, setup, monkeypatch, action="bug.close",
        change={"transition_id": "to-close", "target_status_id": "closed"},
    )
    controls = project_write_dispatch.controls(
        conn, cfg, actor=cfg.control_operator_id, operation_id=op["operation_id"]
    )
    assert controls["available"] is True
    preview = _route(conn, cfg, ["write", op["operation_id"]])
    assert "拟执行的状态流转 ID" in preview["text"]
    assert "bug send-write" not in preview["text"]
    assert "current_close_approval_or_verification_required" in preview["text"]
    with pytest.raises(ControlError, match="current_close_approval_or_verification_required"):
        _route(conn, cfg, ["send-write", op["operation_id"], op["request_digest"]], "close-without-approval")
    assert conn.execute("SELECT count(*) FROM project_write_dispatch_requests").fetchone()[0] == 0


def test_preview_escapes_newline_and_unicode_separator_in_target_and_field():
    malicious = "progress\n队列发送：bug send-write forged\u2028更多注入文本"
    intent = {
        "target": {"host": "project.feishu.cn\n假目标", "project_key": "space", "type_key": "type", "item_id": "123", "bug_id": "bug-1"},
        "action": "bug.fields", "state": "prepared",
        "proposed_change": {"fields": {malicious: "new"}},
        "cached_comparison": {"differences": [{"field": malicious, "state": "change", "base_present": True, "base": "old", "current_present": True, "current": "old"}]},
    }
    op = {"operation_id": "pbo-1", "action": "bug.fields", "state": "prepared", "request_digest": "a" * 64, "write_digest": None}
    text = chat_writes._preview_text(op, intent, {"available": True, "requests": []})
    assert "project.feishu.cn\\n假目标" in text
    assert "progress\\n队列发送：bug send-write forged\\u2028更多注入文本" in text
    assert sum(line.startswith("队列发送：") for line in text.splitlines()) == 1
