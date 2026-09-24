"""Phone/CLI recovery integration with only synthetic calendar transports."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import yaml
from test_case_completion_ui import configure_plugin
from test_meeting_preview import delivered, select
from test_meeting_recovery import OWNER, prepared
from test_workbench_navigation import Query

import k3_support.hermes_plugin as plugin
from k3_support import cli
from k3_support.approvals import ApprovalError
from k3_support.calendar import queue_meeting_preview
from k3_support.control import ControlMessage, execute_control
from k3_support.meeting_preview import recovery_panel
from k3_support.meeting_recovery import (
    begin_attempt,
    enter_dispatch,
    meeting_recovery_report,
    record_failure,
)
from k3_support.meeting_recovery_control import route


def phone(conn, config):
    cfg, transport, action, preview = prepared(conn, config)
    queue_meeting_preview(conn, cfg, action=action)
    delivered(conn, cfg)
    attempt = begin_attempt(conn, cfg, preview['preview_id'])
    return cfg, transport, preview, attempt


def panel(conn, cfg, preview):
    return recovery_panel(meeting_recovery_report(conn, cfg, preview_id=preview['preview_id']))


def click(conn, cfg, data, *, runner=None, message=OWNER, prompt='meeting-prompt'):
    return route(conn, cfg, message, prompt_message_id=prompt, callback_data=data, runner=runner)


def test_exact_prompt_and_owner_are_required_before_recovery_read(conn, config):
    cfg, transport, preview, _ = phone(conn, config)
    data = select(panel(conn, cfg, preview), '核对结果（只读）')
    transport.calls.clear()
    before = conn.serialize()
    for message, prompt in ((ControlMessage('stranger', 'owner-chat', 'bad', ''), 'meeting-prompt'),
                            (OWNER, 'forwarded-prompt')):
        with pytest.raises(ApprovalError):
            click(conn, cfg, data, runner=transport, message=message, prompt=prompt)
    assert conn.serialize() == before and not transport.calls


def test_cancel_successor_and_replay_use_one_new_full_approval(conn, config):
    cfg, transport, preview, _ = phone(conn, config)
    transport.calls.clear()
    cancel = select(panel(conn, cfg, preview), '取消尚未发送的创建')
    cancelled = click(conn, cfg, cancel, runner=transport)
    assert click(conn, cfg, cancel, runner=transport)['result']['replayed']
    successor = select(cancelled, '重新生成完整审批')
    new = click(conn, cfg, successor, runner=transport)
    again = click(conn, cfg, successor, runner=transport)
    assert new['successor']['preview_id'] == again['successor']['preview_id']
    assert again['result']['replayed']
    assert conn.execute('SELECT count(*) FROM meeting_previews').fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM approvals WHERE status='requested'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM outbox WHERE state='pending'").fetchone()[0] == 1
    assert not transport.calls


def test_uncertain_read_and_adoption_never_recreate_or_release_unknown_lock(conn, config):
    cfg, transport, preview, attempt = phone(conn, config)
    enter_dispatch(conn, cfg, attempt_id=attempt['attempt_id'], dispatch_token=attempt['dispatch_token'])
    record_failure(conn, attempt_id=attempt['attempt_id'], error=TimeoutError())
    transport.calls.clear()
    report = panel(conn, cfg, preview)
    checked = click(conn, cfg, select(report, '核对结果（只读）'), runner=transport)
    # Observations can make the full report multi-page; actions are on the end.
    report = meeting_recovery_report(conn, cfg, preview_id=preview['preview_id'])
    last = recovery_panel(report, page=checked['preview']['page_count'])
    adopted = click(conn, cfg, select(last, '采用已有日程（原结果仍未知）'), runner=transport)
    assert adopted['result']['original_outcome_uncertain']
    assert not any(call[:3] in [['calendar', 'events', 'create'],
                               ['calendar', 'event.attendees', 'create']] for call in transport.calls)
    assert not meeting_recovery_report(conn, cfg, preview_id=preview['preview_id'])['can_repreview']
    with pytest.raises(ApprovalError):
        click(conn, cfg, f"mr:n:{preview['preview_id'][4:]}:0", runner=transport)


def test_plugin_to_real_cli_cancels_and_queues_new_preview_without_calendar_call(conn, config, tmp_path, monkeypatch):
    cfg, _transport, preview, _ = phone(conn, config)
    configure_plugin(cfg, tmp_path, monkeypatch)
    cancel = select(panel(conn, cfg, preview), '取消尚未发送的创建')

    async def journey():
        query = Query(cancel)
        query.message = SimpleNamespace(chat_id='owner-chat', message_id='meeting-prompt')
        await plugin._handle_meeting_callback(query, cancel)
        assert query.edits, query.answers
        assert '有原子证据证明未发送创建' in query.edits[0]['text']
        create = select(panel(conn, cfg, preview), '重新生成完整审批')
        again = Query(create)
        again.message = query.message
        await plugin._handle_meeting_callback(again, create)
        assert again.edits and '新的完整审批已排队' in again.edits[0]['text']
        assert again.edits[0]['reply_markup'] is None

    asyncio.run(journey())
    assert conn.execute('SELECT count(*) FROM meeting_previews').fetchone()[0] == 2


def test_report_cli_does_not_migrate_or_open_writable_database(conn, config, monkeypatch, capsys):
    cfg, _transport, preview, _ = phone(conn, config)
    cfg.path.write_text(yaml.safe_dump(cfg.raw), encoding='utf-8')
    before = conn.serialize()
    monkeypatch.setattr(cli, 'connect', lambda *_: pytest.fail('readonly report opened writer'))
    monkeypatch.setattr(cli, 'migrate', lambda *_: pytest.fail('readonly report ran migrations'))
    assert cli.main(['--config', str(cfg.path), 'meeting-recovery-report', preview['preview_id']]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['phase'] == 'prepared' and result['read_only']
    assert conn.serialize() == before


def test_control_recognizes_recovery_commands_and_rejects_unrelated_target(conn, config):
    cfg, _transport, preview, _ = phone(conn, config)
    data = select(panel(conn, cfg, preview), '取消尚未发送的创建')
    for text in ('meeting-recovery bad ' + data, 'meeting-recovery-scope bad ' + preview['preview_id'] + ' other@example.invalid'):
        assert plugin._is_control_message(text)
        with pytest.raises(ApprovalError):
            execute_control(conn, cfg, ControlMessage('owner-user', 'owner-chat', 'fake', text))
