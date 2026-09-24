from __future__ import annotations

import asyncio
import copy
import html
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_meeting_recovery import CalendarFixture
from test_workbench_navigation import Query, buttons

import k3_support.hermes_plugin as plugin
from k3_support.approvals import ApprovalError, decide_approval
from k3_support.calendar import (
    CalendarError,
    create_meeting_preview,
    execute_meeting_create,
    normalize_meeting_action,
    prepare_conversation_meeting,
    queue_meeting_preview,
)
from k3_support.calendar_availability import query_availability, work_window
from k3_support.config import Config
from k3_support.control import ControlMessage
from k3_support.delivery import DeliveryReceipt, claim_outbox, deliver_claimed
from k3_support.lark import CommandResult
from k3_support.meeting_preview import route
from k3_support.store import create_case
from k3_support.timeutil import parse_iso


def configured(config):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"]["calendar"] = True
    raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    raw["timezone"] = "Asia/Kathmandu"
    return Config(raw, config.path)


def action(conn):
    case_id, _ = create_case(
        conn, title="讨论问题", case_type="meeting", severity="P3", confidence=0.98
    )
    return normalize_meeting_action(
        case_id=case_id,
        summary="<会议> & K3",
        start="2027-01-05T14:00:00+05:45",
        end="2027-01-05T14:30:00+05:45",
        attendee_ids=["ou_colleague"],
        description="确认现场问题和下一步",
        timezone="Asia/Kathmandu",
    )


def approve(conn, cfg, preview):
    return decide_approval(
        conn,
        cfg,
        approval_id=preview["approval_id"],
        approve=True,
        approver_user_id="owner-user",
        approver_chat_id="owner-chat",
        message_id="exact-approve",
        decision_text="explicit fixture approval",
        expected_digest=preview["action_digest"],
    )


def delivered(conn, cfg, prompt="meeting-prompt"):
    row = claim_outbox(conn, worker_id="meeting-fixture")
    deliver_claimed(
        conn,
        cfg,
        row,
        telegram_button_runner=lambda *args, **kwargs: DeliveryReceipt(
            prompt, {"ok": True}
        ),
    )
    payload = json.loads(row["payload_json"])
    return payload["buttons"][0]["callback_data"]


def select(panel, label):
    return next(
        value["callback_data"]
        for value in panel["preview"]["buttons"]
        if value["text"] == label
    )


def click(
    conn,
    cfg,
    data,
    *,
    prompt="meeting-prompt",
    external="meeting-callback",
    user="owner-user",
):
    message = ControlMessage(user, "owner-chat", external, "meeting-view " + data)
    return route(conn, cfg, message, prompt_message_id=prompt, callback_data=data)


def test_freebusy_contract_conflicts_bounded_candidates_and_unknown_coverage(
    conn, config
):
    cfg = configured(config)
    value = action(conn)
    calls = []

    def busy(argv):
        calls.append(argv)
        return CommandResult(
            {
                "freebusy_list": [
                    {"start_time": value["start"], "end_time": value["end"]}
                ]
            },
            "user",
            [],
        )

    evidence = query_availability(cfg, value, runner=busy)
    assert evidence["state"] == "conflict" and not evidence["unknown_ids"]
    assert len(evidence["conflicts"]) == 2 and 1 <= len(evidence["alternatives"]) <= 3
    assert {json.loads(argv[-1])["user_id"] for argv in calls} == {
        "ou_owner",
        "ou_colleague",
    }
    for option in evidence["alternatives"]:
        assert (
            parse_iso(evidence["candidate_start"])
            <= parse_iso(option["start"])
            < parse_iso(option["end"])
            <= parse_iso(evidence["candidate_end"])
        )
        assert (
            parse_iso(option["end"]) - parse_iso(option["start"])
        ).total_seconds() == 1800
        assert option["start"].endswith("+05:45")
    unknown = query_availability(
        cfg, {**value, "attendee_ids": ["oc_team"]}, runner=busy
    )
    assert unknown["unknown_ids"] == ["oc_team"] and not unknown["alternatives"]
    for response in (
        CommandResult({}, "user", []),
        CommandResult({"freebusy_list": []}, "bot", []),
        CommandResult(
            {
                "freebusy_list": [
                    {"start_time": "2027-01-05T14:00", "end_time": value["end"]}
                ]
            },
            "user",
            [],
        ),
    ):
        result = query_availability(
            cfg, value, runner=lambda argv, response=response: response
        )
        assert result["state"] == "unknown" and not result["alternatives"]
    cfg.raw["identity"]["feishu_owner_open_id"] = None
    assert (
        query_availability(
            cfg,
            value,
            runner=lambda argv: CommandResult({"freebusy_list": []}, "user", []),
        )["state"]
        == "unknown"
    )


def test_resource_and_overnight_window_use_real_contract_and_explicit_timezone(
    conn, config
):
    cfg = configured(config)
    cfg.raw["work_hours"] = {"start": "22:00", "end": "06:00"}
    value = action(conn) | {
        "start": "2027-01-06T01:00:00+05:45",
        "end": "2027-01-06T01:30:00+05:45",
        "attendee_ids": ["omm_room"],
    }
    start, end = work_window(cfg, value["start"])
    assert start.isoformat() == "2027-01-05T22:00:00+05:45"
    assert end.isoformat() == "2027-01-06T06:00:00+05:45"
    calls = []

    def runner(argv):
        calls.append(json.loads(argv[-1]))
        return CommandResult({"freebusy_list": []}, "user", [])

    result = query_availability(cfg, value, runner=runner)
    assert result["state"] == "available"
    room = next(call for call in calls if "room_id" in call)
    assert room["room_id"] == "omm_room" and "user_id" not in room


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("start", "2027-01-05T13:00:00+05:45"),
        ("attendee_ids", ["ou_other"]),
        ("description", "changed agenda"),
    ],
)
def test_changed_effect_never_uses_old_approval(conn, config, field, replacement):
    cfg = configured(config)
    value = action(conn)
    preview = create_meeting_preview(conn, action=value)
    approve(conn, cfg, preview)
    conn.execute(
        "UPDATE meeting_previews SET action_json=? WHERE preview_id=?",
        (json.dumps(value | {field: replacement}), preview["preview_id"]),
    )
    calls = []
    with pytest.raises(ApprovalError, match="exact action changed"):
        execute_meeting_create(
            conn,
            cfg,
            preview_id=preview["preview_id"],
            runner=lambda argv: calls.append(argv),
        )
    assert not calls


@pytest.mark.parametrize(
    "response", [None, CommandResult({"event_id": "event"}, "bot", [])]
)
def test_uncertain_create_is_durable_and_changed_query_hash_cannot_retry(
    conn, config, response
):
    cfg = configured(config)
    from k3_support.meeting_recovery import bind_meeting_action, revise_bound_action

    transport = CalendarFixture()
    value = bind_meeting_action(cfg, action(conn), runner=transport)
    preview = create_meeting_preview(conn, action=value)
    approve(conn, cfg, preview)
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[:3] == ["calendar", "calendars", "primary"]:
            return transport(argv)
        if response is None:
            raise TimeoutError("response lost after dispatch")
        return response

    with pytest.raises(CalendarError, match="recovery"):
        execute_meeting_create(
            conn, cfg, preview_id=preview["preview_id"], runner=runner
        )
    stored = conn.execute("SELECT * FROM meeting_previews").fetchone()
    assert (
        stored["status"] == "creating"
        and json.loads(stored["remote_result_json"])["outcome"] == "uncertain"
    )
    same = create_meeting_preview(
        conn, action=value | {"availability": {"state": "unknown"}}
    )
    assert same["preview_id"] == preview["preview_id"]
    with pytest.raises(CalendarError, match="unresolved|uncertain"):
        create_meeting_preview(conn, action=revise_bound_action(value | {"summary": "another attempt"}))
    assert len(calls) == 2


def test_alternative_requires_new_card_new_digest_and_new_approval(conn, config):
    cfg = configured(config)
    from k3_support.meeting_recovery import bind_meeting_action

    value = bind_meeting_action(cfg, action(conn), runner=CalendarFixture())
    value["availability"] = query_availability(
        cfg,
        value,
        runner=lambda argv: CommandResult(
            {
                "freebusy_list": [
                    {"start_time": value["start"], "end_time": value["end"]}
                ]
            },
            "user",
            [],
        ),
    )
    original = queue_meeting_preview(conn, cfg, action=value)
    entry = delivered(conn, cfg)
    page = click(conn, cfg, entry)
    while any(button["text"] == "下一页" for button in page["preview"]["buttons"]):
        page = click(conn, cfg, select(page, "下一页"))
    old_confirm = select(page, "知晓冲突，仍创建")
    result = click(conn, cfg, select(page, "选择候选 1"))
    successor = result["successor"]
    assert successor["action_digest"] != original["action_digest"]
    assert "待送达" in result["preview"]["text"]
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?",
            (original["approval_id"],),
        ).fetchone()[0]
        == "revoked"
    )
    with pytest.raises(ApprovalError, match="no longer pending"):
        click(conn, cfg, old_confirm)
    new_entry = delivered(conn, cfg, prompt="new-meeting-prompt")
    with pytest.raises(ApprovalError, match="not bound"):
        click(conn, cfg, new_entry)
    new_page = click(conn, cfg, new_entry, prompt="new-meeting-prompt")
    assert "未发现冲突" in new_page["preview"]["text"]
    assert (
        conn.execute(
            "SELECT count(*) FROM meeting_previews WHERE status='created'"
        ).fetchone()[0]
        == 0
    )


def test_real_plugin_full_preview_exact_approval_and_fake_calendar_receipt(
    conn, config, tmp_path, monkeypatch
):
    cfg = configured(config)
    counter = tmp_path / "fake-calendar-calls.jsonl"
    fake = tmp_path / "fake-calendar"
    fake.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        'argv=sys.argv[1:]\n'
        'if argv[:3]==["calendar","calendars","primary"]:\n'
        ' data={"calendars":[{"user_id":"ou_owner","calendar":{"calendar_id":"fixture_calendar@example.invalid","type":"primary","role":"owner","is_deleted":False,"is_third_party":False}}]}\n'
        'else:\n'
        ' assert argv[:3] in [["calendar","events","create"],["calendar","event.attendees","create"]]\n'
        f' p=Path({str(counter)!r})\n p.write_text((p.read_text() if p.exists() else "")+json.dumps(argv)+"\\n")\n'
        ' body=json.loads(argv[argv.index("--data")+1])\n'
        ' data={"event":{**body,"event_id":"fixture-event","organizer_calendar_id":"fixture_calendar@example.invalid","status":"confirmed","is_exception":False}} if argv[1]=="events" else {"attendees":[{**v,"rsvp_status":"needs_action"} for v in body["attendees"]]}\n'
        'print(json.dumps({"ok":True,"identity":"user","data":data}))\n',
        encoding="utf-8",
    )
    fake.chmod(0o700)
    cfg.raw["runtime"]["lark_cli_command"] = str(fake)
    cfg.path.write_text(yaml.safe_dump(cfg.raw), encoding="utf-8")
    runtime = tmp_path / "meeting-ui-runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "control_cli": str(Path(sys.executable).parent / "k3-supportctl"),
                "control_config": str(cfg.path),
                "timeout_seconds": 10,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("K3_SUPPORT_CONTROL_PLUGIN_CONFIG", str(runtime))
    value = action(conn)
    agenda = "<要保留> 🌬" * 800
    preview = prepare_conversation_meeting(
        conn,
        cfg,
        case_id=value["case_id"],
        content="明天开会",
        requester_id="ou_colleague",
        planner=lambda _: {
            "summary": value["summary"],
            "start": value["start"],
            "end": value["end"],
            "agenda": agenda,
            "include_requester": True,
            "confidence": 0.99,
        },
        availability_runner=lambda argv: CommandResult(
            {"freebusy_list": []}, "user", []
        ),
        calendar_runner=CalendarFixture(),
    )
    entry = delivered(conn, cfg)

    class Adapter:
        async def _handle_callback_query(self, *_):
            raise AssertionError("meeting callback escaped into LLM handling")

    def importer(name):
        if name == "gateway.platform_registry":
            return SimpleNamespace(
                platform_registry=SimpleNamespace(get=lambda _: None)
            )
        if name == "hermes_plugins.telegram_platform.adapter":
            return SimpleNamespace(TelegramAdapter=Adapter)
        raise ImportError(name)

    monkeypatch.setattr(plugin.importlib, "import_module", importer)
    plugin._install_callback_handler()
    adapter = Adapter()
    sequence = 0

    async def press(data, *, prompt="meeting-prompt", user="owner-user"):
        nonlocal sequence
        sequence += 1
        query = Query(data)
        query.id = f"fixture-meeting-{sequence}"
        query.from_user = SimpleNamespace(id=user)
        query.message = SimpleNamespace(chat_id="owner-chat", message_id=prompt)
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), None
        )
        return query

    async def journey():
        before = list(conn.iterdump())
        query = await press(entry)
        assert query.edits, query.answers
        all_text = []
        while True:
            current = buttons(query.edits[0])
            all_text.append(html.unescape(query.edits[0]["text"].split("\n\n", 1)[1]))
            assert len(query.edits[0]["text"].encode("utf-16-le")) // 2 < 4096
            next_button = next(
                (button for button in current if button["text"] == "下一页"), None
            )
            if next_button is None:
                break
            assert not any(
                button["callback_data"].startswith("mt:a:") for button in current
            )
            query = await press(next_button["callback_data"])
        assert agenda in "".join(all_text) and "Asia/Kathmandu" in "".join(all_text)
        assert list(conn.iterdump()) == before and not counter.exists()
        confirm = next(
            button["callback_data"]
            for button in current
            if button["text"] == "确认创建此会议"
        )
        denied = await press(confirm, user="stranger")
        assert not denied.edits
        denied = await press(confirm, prompt="forwarded")
        assert not denied.edits and not counter.exists()
        bypass = await press("k3a:a:" + preview["approval_id"])
        assert not bypass.edits and not counter.exists()
        created = await press(confirm)
        assert "日程及邀请对象已确认" in created.edits[0]["text"]
        assert "fixture-event" in created.edits[0]["text"]
        repeated = await press(confirm)
        assert not repeated.edits

    asyncio.run(journey())
    calls = [json.loads(line) for line in counter.read_text().splitlines()]
    assert len(calls) == 2
    assert json.loads(calls[0][calls[0].index("--data") + 1])["description"] == agenda
    assert calls[0][calls[0].index("--as") + 1] == "user"
    row = conn.execute(
        "SELECT status,calendar_event_id FROM meeting_previews"
    ).fetchone()
    assert row[:] == ("created", "fixture-event")
