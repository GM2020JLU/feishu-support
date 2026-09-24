from datetime import UTC, datetime

import pytest
from conftest import config_data
from test_meeting_preview import action, configured
from test_meeting_recovery import CalendarFixture
from test_review import active_config, make_job, remote_runner

from k3_support.approvals import approval_binding, decide_approval
from k3_support.calendar import prepare_conversation_meeting
from k3_support.config import ConfigError, validate_config
from k3_support.control import ControlMessage, _decide_and_continue
from k3_support.lark import CommandResult
from k3_support.review import run_hermes_review


def independent_identity(cfg):
    cfg.raw["identity"].update(
        control_operator_id="operator",
        telegram_control_user_id=None,
        telegram_control_chat_id=None,
        feishu_control_user_id="tenant-user",
        feishu_control_chat_id="oc_control",
    )
    validate_config(cfg.raw)


@pytest.mark.parametrize("feature", ["board", "wip_push", "calendar"])
def test_control_features_accept_independent_operator(tmp_path, feature):
    data = config_data(tmp_path, mode="active")
    data["features"][feature] = True
    data["identity"].update(
        control_operator_id="operator",
        telegram_control_user_id=None,
        telegram_control_chat_id=None,
    )
    validate_config(data)
    del data["identity"]["control_operator_id"]
    with pytest.raises(ConfigError, match="stable operator"):
        validate_config(data)


@pytest.mark.parametrize(
    "channel,user,chat",
    [("gui", "operator", "web"), ("feishu", "tenant-user", "oc_control")],
)
def test_review_gate_without_telegram_can_be_approved_and_queued(
    conn, config, channel, user, chat
):
    cfg = active_config(config, board=True)
    independent_identity(cfg)
    expected_case = datetime.now(UTC).strftime("K3-%Y%m%d-0001")
    case_id, job_id = make_job(
        conn,
        cfg,
        requested_actions=[
            {
                "type": "board",
                "session_id": expected_case + "-board-1",
                "estimated_minutes": 35,
                "purpose": "RAM boot and fresh serial validation",
            }
        ],
    )
    result = run_hermes_review(
        conn,
        cfg,
        job_id=job_id,
        remote_runner=remote_runner(),
        hermes_runner=lambda *_: pytest.fail("deterministic gate invoked a model"),
    )
    followup = result["followup"]
    assert followup["requested"] and followup["outbox_id"] is None
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    approved = _decide_and_continue(
        conn,
        cfg,
        ControlMessage(user, chat, "approval-event", "explicit approval"),
        approval_id=followup["approval_id"],
        approve=True,
        expected_digest=followup["digest"],
        expected_binding=approval_binding(conn, followup["approval_id"]),
        control_channel=channel,
    )
    assert approved["continuation"]["created"]
    assert (
        conn.execute(
            "SELECT state FROM jobs WHERE job_id=?",
            (approved["continuation"]["job_id"],),
        ).fetchone()[0]
        == "queued"
    )
    row = conn.execute(
        "SELECT approver_identity,approver_channel FROM approvals WHERE case_id=?",
        (case_id,),
    ).fetchone()
    assert tuple(row) == ("operator", channel)


def test_conversation_meeting_without_telegram_keeps_exact_approval(conn, config):
    cfg = configured(config)
    independent_identity(cfg)
    value = action(conn)
    preview = prepare_conversation_meeting(
        conn,
        cfg,
        case_id=value["case_id"],
        content="开会",
        requester_id="ou_colleague",
        planner=lambda _: {
            "summary": value["summary"],
            "start": value["start"],
            "end": value["end"],
            "agenda": "确认现场问题和下一步",
            "include_requester": True,
            "confidence": 0.99,
        },
        availability_runner=lambda _: CommandResult({"freebusy_list": []}, "user", []),
        calendar_runner=CalendarFixture(),
    )
    assert preview and preview["outbox_id"] is None
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    approved = decide_approval(
        conn,
        cfg,
        approval_id=preview["approval_id"],
        approve=True,
        approver_user_id="operator",
        approver_chat_id="web",
        message_id="web-approval",
        decision_text="explicit approval",
        control_channel="gui",
        expected_digest=preview["action_digest"],
        expected_binding=approval_binding(conn, preview["approval_id"]),
    )
    assert approved["status"] == "approved"
