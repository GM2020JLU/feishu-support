"""Independent meeting UI/control probes; all calendars/transports synthetic."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import yaml
from test_meeting_preview import action, configured, delivered, select
from test_meeting_recovery import OWNER, CalendarFixture, prepared
from test_meeting_recovery_control import click, panel, phone

from k3_support import cli, meeting_recovery_control
from k3_support.approvals import ApprovalError
from k3_support.calendar import queue_meeting_preview
from k3_support.meeting_preview import recovery_panel
from k3_support.meeting_recovery import bind_meeting_action, meeting_recovery_report


def test_successor_reserved_before_queue_crash_can_retry_without_second_approval(
    conn, config, monkeypatch
):
    cfg, transport, preview, _ = phone(conn, config)
    cancelled = click(
        conn, cfg, select(panel(conn, cfg, preview), "取消尚未发送的创建"), runner=transport
    )
    callback = select(cancelled, "重新生成完整审批")
    original = meeting_recovery_control.queue_reserved_meeting_preview
    monkeypatch.setattr(
        meeting_recovery_control, "queue_reserved_meeting_preview",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic queue crash")),
    )
    with pytest.raises(RuntimeError, match="synthetic queue crash"):
        click(conn, cfg, callback, runner=transport)
    reserved = meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])
    assert reserved["successor_preview_id"]
    monkeypatch.setattr(meeting_recovery_control, "queue_reserved_meeting_preview", original)
    repaired = click(conn, cfg, callback, runner=transport)
    assert repaired["successor"]["preview_id"] == reserved["successor_preview_id"]
    assert conn.execute("SELECT count(*) FROM meeting_previews").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM outbox WHERE state='pending'").fetchone()[0] == 1


@pytest.mark.parametrize("edit_fails", [False, True])
def test_text_recovery_scope_result_updates_original_bound_card(
    conn, config, monkeypatch, edit_fails
):
    import k3_support.hermes_plugin as plugin

    cfg, transport, original, preview = prepared(conn, config, legacy=True)
    queue_meeting_preview(conn, cfg, action=original)
    delivered(conn, cfg)
    conn.execute("UPDATE meeting_previews SET status='creating' WHERE preview_id=?", (preview["preview_id"],))
    result = meeting_recovery_control.search_legacy_scope(
        conn, cfg, OWNER, prompt_message_id="meeting-prompt",
        preview_id=preview["preview_id"], calendar_id=transport.calendar_id, runner=transport,
    )
    assert result["result"]["classification"] == "legacy_candidate"
    event = SimpleNamespace(
        source=SimpleNamespace(platform="telegram", user_id="owner-user", chat_id="owner-chat"),
        message_id="owner-scope-command",
        text=f"meeting-recovery-scope meeting-prompt {preview['preview_id']} {transport.calendar_id}",
    )
    monkeypatch.setattr(plugin, "_run_control", lambda _: (True, result))
    buttons, receipts = [], []

    def send_buttons(*args, **kwargs):
        buttons.append((args, kwargs))
        assert kwargs == {"edit_message_id": "meeting-prompt"}
        if edit_fails:
            raise plugin.PluginConfigurationError("synthetic edit failure")
        return "meeting-prompt"

    class Adapter:
        async def send(self, chat_id, text, **kwargs):
            receipts.append(text)
            return SimpleNamespace(success=True)

    monkeypatch.setattr(plugin, "_send_button_message", send_buttons)
    asyncio.run(plugin._execute_and_reply(Adapter(), event))
    assert len(buttons) == 1 and buttons[0][0][1] == result["preview"]["text"]
    assert "创建结果未知" in buttons[0][0][1]
    if edit_fails:
        assert receipts and "本地恢复结果已记录" in receipts[0] and "不要重试创建" in receipts[0]
        assert meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])["observation_total"] == 1
    else:
        current = result
        while current["preview"]["page"] < current["preview"]["page_count"]:
            current = click(conn, cfg, select(current, "下一页"), runner=transport)
        adopted = click(conn, cfg, select(current, "采用已有日程（原结果仍未知）"), runner=transport)
        assert adopted["result"]["original_outcome_uncertain"]
        assert adopted["display_target"]["message_id"] == "meeting-prompt"


def test_real_control_cli_invitation_timeout_keeps_created_event_and_blocks_duplicate(
    conn, config, monkeypatch, capsys
):
    cfg, transport = configured(config), CalendarFixture()
    bound = bind_meeting_action(cfg, action(conn), runner=transport)
    transport.action = bound
    preview = queue_meeting_preview(conn, cfg, action=bound)
    delivered(conn, cfg)
    cfg.path.write_text(yaml.safe_dump(cfg.raw), encoding="utf-8")
    transport.calls.clear()
    transport.fail = "calendar event.attendees create"
    monkeypatch.setattr("k3_support.lark.run_json", transport)
    callback = f"mt:a:{preview['preview_id'][4:]}:{preview['action_digest'][:16]}"
    argv = [
        "--config", str(cfg.path), "control", "--control-user-id", "owner-user",
        "--control-chat-id", "owner-chat", "--message-id", "synthetic-confirm",
        "--text", "meeting-view meeting-prompt " + callback,
    ]
    assert cli.main(argv) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["execution"]["requires_recovery"]
    assert "日程已创建，邀请未完整核实" in output["preview"]["text"]
    persisted = meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])
    assert persisted["phase"] == "partial" and persisted["event_id"] == transport.event_id
    before = list(transport.calls)
    assert cli.main(argv) == 2
    capsys.readouterr()
    assert transport.calls == before
    assert sum(call[:3] == ["calendar", "events", "create"] for call in before) == 1
    assert sum(call[:3] == ["calendar", "event.attendees", "create"] for call in before) == 1


def test_delayed_queue_crash_recovery_must_not_replace_reserved_successor(
    conn, config, monkeypatch
):
    cfg, transport, preview, _ = phone(conn, config)
    cancelled = click(
        conn, cfg, select(panel(conn, cfg, preview), "取消尚未发送的创建"), runner=transport
    )
    callback = select(cancelled, "重新生成完整审批")
    original = meeting_recovery_control.queue_reserved_meeting_preview
    monkeypatch.setattr(
        meeting_recovery_control, "queue_reserved_meeting_preview",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic queue crash")),
    )
    with pytest.raises(RuntimeError, match="synthetic queue crash"):
        click(conn, cfg, callback, runner=transport)
    reserved = meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])["successor_preview_id"]
    # A delayed recovery observes the original successor's expired approval.
    # It must not silently invent another preview with the same operation key.
    conn.execute(
        "UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE approval_id=(SELECT approval_id FROM meeting_previews WHERE preview_id=?)",
        (reserved,),
    )
    monkeypatch.setattr(meeting_recovery_control, "queue_reserved_meeting_preview", original)
    try:
        repaired = click(conn, cfg, callback, runner=transport)
    except (ValueError, ApprovalError):
        assert conn.execute("SELECT count(*) FROM meeting_previews").fetchone()[0] == 2
    else:
        assert repaired["successor"]["preview_id"] == reserved, {
            "reserved_successor": reserved,
            "queued_successor": repaired["successor"]["preview_id"],
            "preview_count": conn.execute("SELECT count(*) FROM meeting_previews").fetchone()[0],
        }


def test_real_legacy_scope_cli_adopts_without_inventing_original_calendar_or_success(
    conn, config, monkeypatch, capsys
):
    cfg, transport, original, preview = prepared(conn, config, legacy=True)
    queue_meeting_preview(conn, cfg, action=original)
    delivered(conn, cfg)
    conn.execute("UPDATE meeting_previews SET status='creating' WHERE preview_id=?", (preview["preview_id"],))
    cfg.path.write_text(yaml.safe_dump(cfg.raw), encoding="utf-8")
    transport.calls.clear()
    monkeypatch.setattr("k3_support.lark.run_json", transport)
    monkeypatch.setattr(meeting_recovery_control, "run_json", transport)
    prefix = [
        "--config", str(cfg.path), "control", "--control-user-id", "owner-user",
        "--control-chat-id", "owner-chat", "--message-id", "synthetic-legacy-scope",
    ]
    command = f"meeting-recovery-scope meeting-prompt {preview['preview_id']} {transport.calendar_id}"
    assert cli.main([*prefix, "--text", command]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["target_basis"] == "operator_search_scope"
    assert value["result"]["classification"] == "legacy_candidate"
    report = meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])
    last = recovery_panel(report, page=value["preview"]["page_count"])
    callback = select(last, "采用已有日程（原结果仍未知）")
    assert cli.main([*prefix, "--text", "meeting-recovery meeting-prompt " + callback]) == 0
    adopted = json.loads(capsys.readouterr().out)
    assert adopted["result"]["original_outcome_uncertain"]
    final = conn.execute("SELECT target_json,adopted_target_json,phase FROM meeting_create_attempts WHERE preview_id=?", (preview["preview_id"],)).fetchone()
    assert final["target_json"] is None and final["adopted_target_json"] and final["phase"] == "adopted"
    assert not any(call[:3] in [["calendar", "events", "create"], ["calendar", "event.attendees", "create"]] for call in transport.calls)
