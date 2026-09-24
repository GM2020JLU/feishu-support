"""Synthetic Telegram/control/readonly-CLI integration for stable navigation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from test_case_completion_ui import configure_plugin
from test_workbench_navigation import Query, buttons, make_case

import k3_support.hermes_plugin as plugin
from k3_support import cli
from k3_support.control import ControlMessage, execute_control
from k3_support.workbench import workbench_page


def test_control_keyset_navigation_is_authenticated_and_readonly(conn, config):
    case_id = make_case(conn, "first")
    before = conn.serialize()
    result = execute_control(conn, config, ControlMessage("owner-user", "owner-chat", "read-1", "workbench-nav wb2:open:a"))
    assert result["snapshot"]["items"][0]["case_id"] == case_id
    assert conn.serialize() == before
    with pytest.raises(Exception, match="identity"):
        execute_control(conn, config, ControlMessage("stranger", "owner-chat", "read-2", "workbench-nav wb2:open:a"))


def test_telegram_navigation_roundtrip_keeps_origin_after_action(conn, config, tmp_path, monkeypatch):
    from k3_support.coordination import ensure_turn
    from k3_support.store import ingest_event
    from k3_support.timeutil import iso_now

    for index in range(9):
        case_id = make_case(conn, index)
        event_pk, _ = ingest_event(conn, source='feishu_user_poll', identity='user',
                                   external_id=f'om_navigation_{index}', payload={'content': '启动失败', 'chat_type': 'p2p'},
                                   sender_id='ou_fixture', chat_id=f'oc_navigation_{index}', occurred_at=iso_now())
        ensure_turn(conn, case_id=case_id, source_event_pk=event_pk)
    configure_plugin(config, tmp_path, monkeypatch)
    opening = workbench_page(conn, config)
    second_cursor = opening["snapshot"]["next_cursor"]

    async def exercise():
        second = Query(second_cursor)
        await plugin._handle_workbench_callback(second, second_cursor)
        assert second.edits, second.answers
        assert "本页" in second.answers[-1]["text"]
        item = next(b["callback_data"] for b in buttons(second.edits[0]) if b["callback_data"].startswith("wi2:"))
        detail = Query(item)
        await plugin._handle_workbench_callback(detail, item)
        assert detail.edits, detail.answers
        action = next(b["callback_data"] for b in buttons(detail.edits[0]) if b["callback_data"].startswith("wka2:c:"))
        claim = Query(action)
        claim.id = "claim-on-second-segment"
        claim.message = SimpleNamespace(chat_id="owner-chat", message_id="detail-message", reply_markup=detail.edits[0]["reply_markup"])
        await plugin._handle_workbench_callback(claim, action)
        assert claim.edits, claim.answers
        origin = next(b["callback_data"] for b in buttons(claim.edits[0]) if b["text"] == "返回工作台")
        assert origin == second_cursor
        back = Query(origin)
        await plugin._handle_workbench_callback(back, origin)
        assert back.edits and "case-4" in back.edits[0]["text"]

    asyncio.run(exercise())


@pytest.mark.parametrize("data", ["wkb:all:1:refresh", "wki:all:1:1:1234567890abcdef", "wkd:K3-fixture:1234567890abcdef:1"])
def test_legacy_ordinal_buttons_never_reach_authoritative_control(monkeypatch, data):
    monkeypatch.setattr(plugin, "_run_control", lambda *_: pytest.fail("legacy ordinal must not be reinterpreted"))
    query = Query(data)
    asyncio.run(plugin._handle_workbench_callback(query, data))
    assert not query.edits and query.answers[-1]["show_alert"]
    assert "刷新" in query.answers[-1]["text"]


def test_readonly_workbench_cli_uses_cursor_not_migration(conn, config, tmp_path, monkeypatch, capsys):
    import json

    import yaml

    for index in range(7):
        make_case(conn, index)
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    before = conn.serialize()
    monkeypatch.setattr(cli, "connect", lambda *_: pytest.fail("reader must not open writable connection"))
    monkeypatch.setattr(cli, "migrate", lambda *_: pytest.fail("reader must not migrate"))
    assert cli.main(["--config", str(config.path), "workbench", "--limit", "4"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["next_cursor"]
    assert cli.main(["--config", str(config.path), "workbench", "--limit", "4", "--cursor", first["next_cursor"]]) == 0
    second = json.loads(capsys.readouterr().out)
    assert len(second["items"]) == 3
    assert conn.serialize() == before
