"""Local receipt/control journeys; no real messaging or hardware transports."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_coordination import active_config, make_turn, queue_reply
from test_workbench_navigation import Query, buttons

import k3_support.hermes_plugin as plugin
from k3_support.approvals import request_approval
from k3_support.case_detail import case_detail
from k3_support.control import ControlMessage, execute_control
from k3_support.coordination import control_communication, ensure_turn
from k3_support.db import transaction
from k3_support.delivery import claim_outbox, deliver_claimed
from k3_support.delivery_attempts import start_attempt
from k3_support.delivery_recovery import record_blocked_delivery
from k3_support.lark import CommandResult
from k3_support.store import enqueue_outbox, ingest_event
from k3_support.workbench import workbench_snapshot


def answered_faq(conn, config, *, index=""):
    cfg = active_config(config)
    message_id = f"om_question{index}"
    case_id, event_pk, turn = make_turn(
        conn, message_id=message_id, chat_id=f"oc_chat{index}"
    )
    conn.execute(
        "UPDATE cases SET type='faq',state='answering' WHERE case_id=?", (case_id,)
    )
    conn.execute(
        "UPDATE inbound_events SET status='processed' WHERE event_pk=?", (event_pk,)
    )
    outbox_id = queue_reply(conn, cfg, case_id, event_pk, message_id=message_id)
    claimed = claim_outbox(conn, worker_id="local-faq-receipt")
    assert claimed["outbox_id"] == outbox_id

    def fake_lark(argv):
        return CommandResult(
            {"messages": [], "has_more": False}
            if "+chat-messages-list" in argv
            else {"message_id": f"om_faq_answer{index}"},
            "user",
            [],
        )

    assert (
        deliver_claimed(conn, cfg, claimed, lark_runner=fake_lark).remote_id
        == f"om_faq_answer{index}"
    )
    return cfg, case_id, event_pk, turn, outbox_id


def configure_plugin(config, tmp_path, monkeypatch):
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    runtime = tmp_path / "completion-plugin.json"
    runtime.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "control_cli": str(Path(sys.executable).parent / "k3-supportctl"),
                "control_config": str(config.path),
                "timeout_seconds": 10,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("K3_SUPPORT_CONTROL_PLUGIN_CONFIG", str(runtime))


def test_answered_faq_leaves_pending_without_claiming_field_resolution(conn, config):
    cfg, case_id, _, _, _ = answered_faq(conn, config)
    before = list(conn.iterdump())
    pending = workbench_snapshot(conn, config=cfg)
    assert not any(item["case_id"] == case_id for item in pending["items"])
    assert pending["counts"]["answered_faq"] == 1
    assert pending["all_items"] == 0
    history = workbench_snapshot(conn, config=cfg, view="closed")
    assert [(item["case_id"], item["kind"]) for item in history["items"]] == [
        (case_id, "answered_faq")
    ]
    assert "不代表现场解决" in history["items"][0]["waiting_for"]
    assert list(conn.iterdump()) == before
    case = conn.execute(
        "SELECT state,outcome,resolved_at FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    assert tuple(case) == ("monitoring", "answered", None)


@pytest.mark.parametrize(
    "blocker",
    [
        "unprocessed_source",
        "new_turn",
        "pending_unlinked_input",
        "dead_letter_input",
        "queued_job",
        "running_job",
        "waiting_job",
        "failed_job",
        "orphaned_job",
        "requested_approval",
        "approved_approval",
        "pending_outbox",
        "retry_outbox",
        "sending_outbox",
        "failed_outbox",
        "device_lock",
        "changed_fence",
        "wrong_round_receipt",
        "wrong_source_receipt",
        "no_receipt",
        "unknown_provenance",
        "uncertain_cancelled_send",
    ],
)
def test_answered_faq_keeps_every_unfinished_current_round_obligation_visible(
    conn, config, blocker
):
    cfg, case_id, event_pk, turn, outbox_id = answered_faq(conn, config)
    now = datetime.now(UTC).isoformat()
    if blocker == "unprocessed_source":
        conn.execute(
            "UPDATE inbound_events SET status='new' WHERE event_pk=?", (event_pk,)
        )
    elif blocker in {"new_turn", "pending_unlinked_input", "dead_letter_input"}:
        followup, _ = ingest_event(
            conn,
            source="feishu_user_poll",
            identity="user",
            external_id="om_followup",
            payload={"content": "这个命令报错了", "chat_type": "p2p"},
            occurred_at=now,
            sender_id="ou_colleague",
            chat_id="oc_chat",
        )
        if blocker == "new_turn":
            ensure_turn(conn, case_id=case_id, source_event_pk=followup)
            conn.execute(
                "UPDATE inbound_events SET status='processed' WHERE event_pk=?",
                (followup,),
            )
        elif blocker == "dead_letter_input":
            conn.execute(
                "UPDATE inbound_events SET status='dead_letter' WHERE event_pk=?",
                (followup,),
            )
    elif blocker.endswith("_job"):
        state = blocker.removesuffix("_job")
        conn.execute(
            """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,created_at,updated_at)
               VALUES('unfinished-job',?,'codex',?,'fixture',?,?,?)""",
            (case_id, state, now, now, now),
        )
    elif blocker.endswith("_approval"):
        approval = request_approval(
            conn,
            approval_type="board1_lease",
            case_id=case_id,
            action={"case_id": case_id, "fixture": True},
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        if blocker == "approved_approval":
            conn.execute(
                "UPDATE approvals SET status='approved' WHERE approval_id=?",
                (approval[0],),
            )
    elif blocker.endswith("_outbox"):
        pending_id, _ = enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="reply",
            destination="om_question",
            payload={"text": "not sent"},
            idempotency_key="unfinished-send",
            case_id=case_id,
        )
        state = {"failed": "permanent_failure"}.get(
            blocker.removesuffix("_outbox"), blocker.removesuffix("_outbox")
        )
        conn.execute("UPDATE outbox SET state=? WHERE outbox_id=?", (state, pending_id))
    elif blocker == "device_lock":
        conn.execute(
            "INSERT INTO locks(lock_key,owner,scope,case_id,acquired_at,heartbeat_at,expires_at) VALUES('board1','session','board',?,?,?,?)",
            (case_id, now, now, now),
        )
    elif blocker == "changed_fence":
        conn.execute(
            "UPDATE conversation_turns SET fence=fence+1 WHERE turn_id=?",
            (turn["turn_id"],),
        )
    elif blocker == "wrong_round_receipt":
        conn.execute(
            "UPDATE outbox SET lifecycle_round=0 WHERE outbox_id=?", (outbox_id,)
        )
    elif blocker == "wrong_source_receipt":
        conn.execute(
            "UPDATE outbox SET source_event_pk=NULL WHERE outbox_id=?", (outbox_id,)
        )
    elif blocker == "no_receipt":
        conn.execute(
            "UPDATE outbox SET remote_message_id=NULL WHERE outbox_id=?", (outbox_id,)
        )
    elif blocker == "uncertain_cancelled_send":
        enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="reply",
            destination="om_question",
            payload={"text": "uncertain"},
            idempotency_key="uncertain-attempt",
            case_id=case_id,
        )
        claimed = claim_outbox(conn, worker_id="fixture-uncertain")
        assert start_attempt(conn, claimed)
        conn.execute(
            "UPDATE outbox SET state='cancelled' WHERE outbox_id=?",
            (claimed["outbox_id"],),
        )
    else:
        conn.execute(
            "UPDATE cases SET outcome_provenance='unknown' WHERE case_id=?", (case_id,)
        )
    conn.commit()
    before = list(conn.iterdump())
    snapshot = workbench_snapshot(conn, config=cfg)
    assert any(
        item["case_id"] == case_id and item["item_id"].startswith("case:")
        for item in snapshot["items"]
    )
    assert snapshot["counts"]["answered_faq"] == 0
    if blocker == "new_turn":
        detail = case_detail(conn, case_id=case_id)["preview"]["text"]
        assert "最新已关联消息" in detail and "这个命令报错了" in detail
        assert "还有新输入" in detail
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize(
    "kind,outcome",
    [
        ("bug", "awaiting_validation"),
        ("bug", "awaiting_environment_comparison"),
        ("faq", "unknown"),
    ],
)
def test_faq_projection_does_not_hide_unvalidated_bugs_or_unknown_answers(
    conn, config, kind, outcome
):
    cfg, case_id, _, _, _ = answered_faq(conn, config)
    conn.execute(
        "UPDATE cases SET type=?,outcome=? WHERE case_id=?", (kind, outcome, case_id)
    )
    result = workbench_snapshot(conn, config=cfg)
    assert any(item["case_id"] == case_id for item in result["items"])
    assert result["counts"]["answered_faq"] == 0


@pytest.mark.parametrize(
    "relation,hidden",
    [
        ("p2p_other_chat", True),
        ("group_other_thread", True),
        ("group_same_thread", False),
        ("group_same_thread_other_sender", False),
    ],
)
def test_pending_input_projection_uses_conversation_scope_without_collecting_more(
    conn, config, relation, hidden
):
    cfg, _, _, turn, _ = answered_faq(conn, config)
    if relation.startswith("group"):
        conn.execute(
            "UPDATE conversation_turns SET chat_type='group',thread_id='omt_tracked' WHERE turn_id=?",
            (turn["turn_id"],),
        )
    ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_pending",
        payload={
            "content": "followup",
            "chat_type": "group" if relation.startswith("group") else "p2p",
        },
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_other_colleague"
        if relation.endswith("other_sender")
        else "ou_colleague",
        chat_id="oc_unrelated" if relation == "p2p_other_chat" else "oc_chat",
        thread_id="omt_tracked"
        if relation.startswith("group_same_thread")
        else "omt_unrelated",
    )
    before = list(conn.iterdump())
    result = workbench_snapshot(conn, config=cfg)
    assert result["counts"]["answered_faq"] == int(hidden)
    assert list(conn.iterdump()) == before


def test_historical_or_finished_jobs_and_expired_approvals_do_not_leave_faq_pending(
    conn, config
):
    cfg, case_id, _, _, _ = answered_faq(conn, config)
    now = datetime.now(UTC).isoformat()
    for index, state in enumerate(("succeeded", "cancelled", "failed")):
        conn.execute(
            """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,created_at,updated_at)
               VALUES(?,?,'codex',?,?,?, ?,?)""",
            (f"past-job-{index}", case_id, state, f"finished-{index}", now, now, now),
        )
    conn.execute("UPDATE jobs SET lifecycle_round=0 WHERE job_id='past-job-2'")
    request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        action={"fixture": True},
        expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    result = workbench_snapshot(conn, config=cfg)
    assert result["counts"]["answered_faq"] == 1
    assert not any(item["item_id"] == f"case:{case_id}" for item in result["items"])


def test_delivery_block_is_owner_decision_without_changing_board_execution(
    conn, config
):
    cfg = active_config(config)
    case_id, event_pk, _ = make_turn(conn)
    conn.execute("UPDATE cases SET state='board_testing' WHERE case_id=?", (case_id,))
    queue_reply(conn, cfg, case_id, event_pk)
    claimed = claim_outbox(conn, worker_id="fixture-block")
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,created_at,updated_at)
           VALUES('live-debug',?,'codex','running','fixture',?,?,?)""",
        (case_id, now, now, now),
    )
    conn.execute(
        "INSERT INTO locks(lock_key,owner,scope,case_id,acquired_at,heartbeat_at,expires_at) VALUES('board1','current-session','board',?,?,?,?)",
        (case_id, now, now, now),
    )
    with transaction(conn):
        conn.execute(
            "UPDATE outbox SET state='cancelled',suppression_reason='knowledge_release:fixture' WHERE outbox_id=?",
            (claimed["outbox_id"],),
        )
        assert record_blocked_delivery(
            conn, cfg, claimed, reason="knowledge_release:fixture"
        )
    snapshot = workbench_snapshot(conn, config=cfg, view="needs_me")
    item = next(
        item for item in snapshot["items"] if item["item_id"] == f"case:{case_id}"
    )
    assert item["kind"] == "owner_decision" and item["state"] == "board_testing"
    assert item["next_action"] and snapshot["counts"]["owner_decision"] == 1
    detail = case_detail(conn, case_id=case_id)["preview"]["text"]
    assert "等待谁：你决策" in detail and "knowledge_release:fixture" in detail
    panel = execute_control(
        conn, cfg, ControlMessage("owner-user", "owner-chat", "blocked-menu", "/feishu")
    )
    assert "待你判断：1" in panel["text"] and "AI 处理中：0" in panel["text"]
    assert (
        conn.execute("SELECT state FROM jobs WHERE job_id='live-debug'").fetchone()[0]
        == "running"
    )
    assert (
        conn.execute("SELECT owner FROM locks WHERE lock_key='board1'").fetchone()[0]
        == "current-session"
    )
    control_communication(
        conn,
        case_id=case_id,
        action="claim",
        actor_id="owner-user",
        external_id="claim-after-block",
    )
    current = workbench_snapshot(conn, config=cfg, view="human")
    assert any(item["case_id"] == case_id for item in current["items"])
    assert current["counts"]["owner_decision"] == 0


def test_mobile_taken_over_case_can_resolve_without_reopening_or_reauthorizing(
    conn, config, tmp_path, monkeypatch
):
    case_id, _, _ = make_turn(conn)
    execute_control(
        conn,
        config,
        ControlMessage("owner-user", "owner-chat", "takeover", f"takeover {case_id} 1"),
    )
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO locks(lock_key,owner,scope,case_id,acquired_at,heartbeat_at,expires_at) VALUES('board1','old-session','board',?,?,?,?)",
        (case_id, now, now, now),
    )
    conn.commit()
    configure_plugin(config, tmp_path, monkeypatch)
    preview = case_detail(conn, case_id=case_id)["preview"]
    resolve = next(
        item["callback_data"]
        for item in preview["buttons"]
        if item["text"] == "标记解决"
    )
    assert not any(
        "重新打开" in item["text"] or item["text"] == "交给 AI"
        for item in preview["buttons"]
    )
    original_locks = [tuple(row) for row in conn.execute("SELECT * FROM locks")]
    original_round = conn.execute(
        "SELECT lifecycle_round FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()[0]

    async def click(*, user="owner-user", chat="owner-chat", identifier="resolve"):
        query = Query(resolve)
        query.id = identifier
        query.from_user = SimpleNamespace(id=user)
        query.message = SimpleNamespace(chat_id=chat, message_id="takeover-detail")
        await plugin._handle_workbench_callback(query, resolve)
        return query

    async def exercise():
        before = list(conn.iterdump())
        for user, chat in (("stranger", "owner-chat"), ("owner-user", "other-chat")):
            rejected = await click(user=user, chat=chat)
            assert not rejected.edits and "身份不匹配" in rejected.answers[-1]["text"]
            assert list(conn.iterdump()) == before
        accepted = await click()
        assert accepted.edits and "operator_resolved" in accepted.edits[0]["text"]
        after = list(conn.iterdump())
        repeated = await click(identifier="repeated")
        assert not repeated.edits and "已有更新" in repeated.answers[-1]["text"]
        assert list(conn.iterdump()) == after

    asyncio.run(exercise())
    case = conn.execute(
        "SELECT state,lifecycle_round,owner FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    assert tuple(case) == ("resolved", original_round, "operator")
    assert [tuple(row) for row in conn.execute("SELECT * FROM locks")] == original_locks
    assert not conn.execute(
        "SELECT 1 FROM conversation_turns WHERE communication_owner='ai'"
    ).fetchone()
    assert not conn.execute(
        "SELECT 1 FROM jobs WHERE state IN ('queued','running','waiting')"
    ).fetchone()


def test_completion_projection_is_read_only_even_on_sqlite_readonly_handle(
    conn, config
):
    cfg, case_id, _, _, _ = answered_faq(conn, config)
    conn.commit()
    readonly = sqlite3.connect(f"file:{cfg.database_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    try:
        before = list(readonly.iterdump())
        assert workbench_snapshot(readonly, config=cfg)["counts"]["answered_faq"] == 1
        assert (
            "不代表现场解决"
            in case_detail(readonly, case_id=case_id)["preview"]["text"]
        )
        assert list(readonly.iterdump()) == before
    finally:
        readonly.close()


def test_more_than_one_hundred_completed_faqs_do_not_dominate_pending_counts(
    conn, config
):
    completed = {
        answered_faq(conn, config, index=str(index))[1] for index in range(105)
    }
    waiting, _, _ = make_turn(
        conn, message_id="om_new_problem", chat_id="oc_new_problem"
    )
    result = workbench_snapshot(conn, config=config)
    assert result["all_items"] == result["total_items"] == 1
    assert result["counts"]["answered_faq"] == 105
    assert result["items"][0]["case_id"] == waiting
    history = workbench_snapshot(conn, config=config, view="closed", limit=50)
    assert history["total_items"] == 105 and history["page_count"] == 3
    observed = set()
    for page in range(1, 4):
        observed.update(
            item["case_id"]
            for item in workbench_snapshot(
                conn,
                config=config,
                view="closed",
                limit=50,
                page=page,
                expected_digest=history["snapshot_digest"],
            )["items"]
        )
    assert observed == completed


def test_mobile_faq_history_and_main_menu_share_the_same_completion_projection(
    conn, config, tmp_path, monkeypatch
):
    cfg, case_id, _, _, _ = answered_faq(conn, config)
    configure_plugin(cfg, tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        plugin,
        "_send_button_message",
        lambda *args: sent.append(args) or "menu-message",
    )

    class Adapter:
        async def send(self, *args, **kwargs):
            raise AssertionError("unexpected messaging fallback")

    async def exercise():
        event = SimpleNamespace(
            text="/feishu",
            message_id="completed-faq-menu",
            source=SimpleNamespace(
                platform="telegram", user_id="owner-user", chat_id="owner-chat"
            ),
        )
        await plugin._execute_and_reply(Adapter(), event)
        assert "AI 处理中：0" in sent[0][1]
        assert "资料咨询已答：1" in sent[0][1]
        entry = next(
            item["callback_data"]
            for item in sent[0][2]
            if item["text"] == "待处理工作台"
        )
        first = Query(entry)
        await plugin._handle_global_callback(first, entry)
        assert first.edits and case_id not in first.edits[0]["text"]
        history = next(
            item["callback_data"]
            for item in buttons(first.edits[0])
            if "已结束" in item["text"]
        )
        past = Query(history)
        await plugin._handle_workbench_callback(past, history)
        assert (
            case_id in past.edits[0]["text"] and "资料咨询已答" in past.edits[0]["text"]
        )
        detail_button = next(
            item["callback_data"]
            for item in buttons(past.edits[0])
            if item["callback_data"].startswith("wi2:")
        )
        detail = Query(detail_button)
        await plugin._handle_workbench_callback(detail, detail_button)
        assert "不代表现场解决" in detail.edits[0]["text"]
        assert not any("重新打开" in item["text"] for item in buttons(detail.edits[0]))

    asyncio.run(exercise())
