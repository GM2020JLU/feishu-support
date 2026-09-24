from __future__ import annotations

import asyncio
import copy
import html
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_review as review_fixtures
import yaml
from test_coordination import active_config, make_turn, queue_reply
from test_mail_calendar_base import configured
from test_workbench_navigation import Query, buttons

import k3_support.hermes_plugin as plugin
from k3_support.approvals import (
    ApprovalError,
    decide_approval,
    normalized_board_action,
    request_approval,
)
from k3_support.base_sync import sync_case
from k3_support.case_detail import case_detail
from k3_support.config import Config
from k3_support.control import (
    ControlError,
    ControlMessage,
    execute_case_callback,
    execute_control,
)
from k3_support.coordination import (
    bind_ai_communication,
    control_communication,
    ensure_turn,
)
from k3_support.db import migrate, migration_files
from k3_support.delivery import claim_outbox, deliver_claimed
from k3_support.executors import ExecutionResult, ExecutorError, create_codex_job
from k3_support.lark import CommandResult
from k3_support.lifecycle import (
    LifecycleError,
    operator_transition,
    record_reply_delivered,
)
from k3_support.retrieval import create_retrieval_job, retrieval_input_for_case
from k3_support.review import (
    handle_codex_completion,
    prepare_codex_review,
    run_hermes_review,
)
from k3_support.store import create_case, enqueue_outbox, ingest_event
from k3_support.workbench import workbench_snapshot


def action(conn, case_id, operation, *, key=None, version=None):
    version = (
        version
        or conn.execute(
            "SELECT version FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()[0]
    )
    return operator_transition(
        conn,
        case_id=case_id,
        action=operation,
        expected_version=version,
        actor_id="owner-user",
        idempotency_key=key or f"{operation}:{version}",
    )


def test_legacy_resolution_has_unknown_provenance_and_projection_does_not_reset_progress():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    for version, name, sql in migration_files():
        if version >= 26:
            continue
        db.executescript(sql)
        db.execute(
            "INSERT INTO schema_migrations VALUES(?,?,?)",
            (version, name, "2026-09-01T00:00:00+00:00"),
        )
    case_id, _ = create_case(
        db, title="legacy FAQ", case_type="faq", severity="P3", confidence=0.9
    )
    db.execute(
        "UPDATE cases SET state='resolved',resolved_at='2026-09-01T01:00:00+00:00' WHERE case_id=?",
        (case_id,),
    )
    events = db.execute("SELECT count(*) FROM case_events").fetchone()[0]
    migrate(db)
    row = db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    assert (
        row["state"] == "resolved"
        and row["outcome"] == "unknown"
        and row["outcome_provenance"] == "legacy_unknown"
    )
    progress = row["last_material_progress_at"]
    db.execute(
        "UPDATE cases SET updated_at='2099-01-01T00:00:00+00:00' WHERE case_id=?",
        (case_id,),
    )
    assert (
        db.execute(
            "SELECT last_material_progress_at FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        == progress
    )
    assert db.execute("SELECT count(*) FROM case_events").fetchone()[0] == events
    db.close()


def test_reopen_is_fenced_human_round_without_old_work_or_event_replay_authority(
    conn, config
):
    cfg = active_config(config)
    case_id, event_pk, turn = make_turn(conn)
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (case_id,))
    old_job, _ = create_retrieval_job(
        conn, cfg, case_id=case_id,
        query=retrieval_input_for_case(conn, case_id=case_id)["full_query"],
        source_event_pk=event_pk
    )
    old_reply = queue_reply(conn, cfg, case_id, event_pk)
    approval_id, _, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        action={"case_id": case_id},
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    current_turn = conn.execute(
        "SELECT * FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)
    ).fetchone()
    prompt, _ = enqueue_outbox(
        conn,
        channel="telegram",
        action_type="owner_decision",
        destination="owner-chat",
        payload={
            "case_id": case_id,
            "control_turn_id": turn["turn_id"],
            "control_fence": current_turn["fence"],
            "buttons": [{"callback_data": f"k3c:c:{case_id}", "text": "claim"}],
        },
        idempotency_key="old-card",
        case_id=case_id,
    )
    conn.execute(
        "UPDATE outbox SET state='delivered',remote_message_id='old-prompt' WHERE outbox_id=?",
        (prompt,),
    )
    resolved = action(conn, case_id, "resolve")
    reopened = action(conn, case_id, "reopen", key="reopen-1")
    assert (
        reopened["lifecycle_round"] == 2
        and reopened["state"] == "triage"
        and reopened["outcome"] == "unknown"
    )
    assert (
        conn.execute("SELECT state FROM jobs WHERE job_id=?", (old_job,)).fetchone()[0]
        == "cancelled"
    )
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE outbox_id=?", (old_reply,)
        ).fetchone()[0]
        == "cancelled"
    )
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?",
            (approval_id,),
        ).fetchone()[0]
        == "revoked"
    )
    current = ensure_turn(conn, case_id=case_id, source_event_pk=event_pk)
    assert current["turn_id"] == turn["turn_id"] and current["fence"] > turn["fence"]
    assert (
        current["communication_owner"] == "human" and current["state"] == "human_hold"
    )
    assert (
        bind_ai_communication(conn, cfg, case_id=case_id, source_event_pk=event_pk)
        is None
    )
    repeated = action(
        conn, case_id, "reopen", key="reopen-1", version=resolved["version"]
    )
    assert repeated["replayed"] and repeated["lifecycle_round"] == 2
    with pytest.raises(LifecycleError, match="stale"):
        action(
            conn, case_id, "reopen", key="another-click", version=resolved["version"]
        )
    with pytest.raises(ControlError, match="authority round"):
        execute_case_callback(
            conn,
            cfg,
            ControlMessage("owner-user", "owner-chat", "stale-click", ""),
            action="claim",
            case_id=case_id,
            prompt_message_id="old-prompt",
        )
    for sql, target in (
        ("UPDATE jobs SET state='queued' WHERE job_id=?", old_job),
        ("UPDATE outbox SET state='retry' WHERE outbox_id=?", old_reply),
        (
            "UPDATE approvals SET status='approved' WHERE approval_id=?",
            approval_id,
        ),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="stale Case round"):
            conn.execute(sql, (target,))
    later, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_new_after_reopen",
        payload={"content": "still fails", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        chat_id="oc_chat",
    )
    new_turn = ensure_turn(conn, case_id=case_id, source_event_pk=later)
    assert new_turn["communication_owner"] == "human"
    control_communication(
        conn,
        case_id=case_id,
        action="delegate",
        actor_id="owner-user",
        external_id="fresh-delegate",
    )
    fresh_input = retrieval_input_for_case(conn, case_id=case_id, project=True)
    new_job, created = create_retrieval_job(
        conn, cfg, case_id=case_id, query=fresh_input["full_query"],
        source_event_pk=fresh_input["source_event_pk"],
        context_binding=fresh_input["context_binding"],
    )
    assert created and new_job != old_job
    assert (
        conn.execute(
            "SELECT lifecycle_round FROM jobs WHERE job_id=?", (new_job,)
        ).fetchone()[0]
        == 2
    )
    assert workbench_snapshot(conn, config=cfg, view="ai")["total_items"] == 1


@pytest.mark.parametrize(
    "kind,board,expected",
    [
        ("faq", False, "answered"),
        ("bug", False, "awaiting_validation"),
        ("bug", True, "awaiting_environment_comparison"),
    ],
)
def test_reply_receipt_never_resolves_field_problem(
    conn, config, kind, board, expected
):
    cfg = active_config(config)
    case_id, event_pk, _ = make_turn(conn)
    conn.execute(
        "UPDATE cases SET type=?,state='answering' WHERE case_id=?", (kind, case_id)
    )
    outbox_id = queue_reply(conn, cfg, case_id, event_pk)
    row = claim_outbox(conn, worker_id="fixture-reply")
    assert row["outbox_id"] == outbox_id
    if board:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO evidence(evidence_id,case_id,evidence_layer,freshness_at,visibility,claim,result,created_at) VALUES('board-proof',?,'ram_boot',?,'internal','board1','本板未复现',?)",
            (case_id, now, now),
        )

    def runner(argv):
        return CommandResult(
            {"messages": [], "has_more": False}
            if "+chat-messages-list" in argv
            else {"message_id": "om_sent"},
            "user",
            [],
        )

    assert deliver_claimed(conn, cfg, row, lark_runner=runner).remote_id == "om_sent"
    case = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    assert (
        case["state"] == "monitoring"
        and case["outcome"] == expected
        and case["resolved_at"] is None
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM outbox WHERE action_type='clarify'"
        ).fetchone()[0]
        == 0
    )
    assert not record_reply_delivered(
        conn,
        row=row,
        remote_message_id="om_sent",
        delivered_at=datetime.now(UTC).isoformat(),
    )
    action(conn, case_id, "resolve")
    action(conn, case_id, "reopen")
    assert not record_reply_delivered(
        conn,
        row=row,
        remote_message_id="om_late",
        delivered_at=datetime.now(UTC).isoformat(),
    )
    assert (
        conn.execute(
            "SELECT outcome FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        == "unknown"
    )


def test_old_delegate_message_replay_does_not_start_reopened_round(conn, config):
    raw = copy.deepcopy(config.raw)
    raw["features"]["codex"] = True
    cfg = Config(raw, config.path)
    case_id, _, _ = make_turn(conn)
    original = ControlMessage(
        "owner-user", "owner-chat", "prior-delegate", f"delegate {case_id}"
    )
    execute_control(conn, cfg, original)
    action(conn, case_id, "resolve")
    action(conn, case_id, "reopen")
    before = list(conn.iterdump())
    result = execute_control(conn, cfg, original)
    assert result["replayed"] and result["turn"]["communication_owner"] == "human"
    assert "continuation" not in result and list(conn.iterdump()) == before


def test_actual_telegram_lifecycle_controls_reject_old_or_unauthorized_clicks(
    conn, config, tmp_path, monkeypatch
):
    case_id, _, _ = make_turn(conn)
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    runtime = tmp_path / "lifecycle-runtime.json"
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
    first = case_detail(conn, case_id=case_id)["preview"]
    resolve = next(
        item["callback_data"] for item in first["buttons"] if item["text"] == "标记解决"
    )
    old_delegate = next(
        item["callback_data"] for item in first["buttons"] if item["text"] == "交给 AI"
    )

    async def click(data, number, *, user="owner-user"):
        query = Query(data)
        query.id = f"lifecycle-click-{number}"
        query.from_user = SimpleNamespace(id=user)
        await plugin._handle_workbench_callback(query, data)
        return query

    async def exercise():
        rejected = await click(resolve, 0, user="stranger")
        assert not rejected.edits
        resolved = await click(resolve, 1)
        assert "operator_resolved" in resolved.edits[0]["text"]
        reopen = next(
            item["callback_data"]
            for item in buttons(resolved.edits[0])
            if "重新打开" in item["text"]
        )
        reopened = await click(reopen, 2)
        assert "human / silent" in reopened.edits[0]["text"]
        old = await click(old_delegate, 3)
        assert not old.edits and "已有更新" in old.answers[-1]["text"]
        before = list(conn.iterdump())
        again = await click(reopen, 4)
        assert not again.edits and list(conn.iterdump()) == before
        claim = next(
            item["callback_data"]
            for item in buttons(reopened.edits[0])
            if item["text"] == "我来回复"
        )
        claimed = await click(claim, 5)
        assert claimed.edits
        delegate = next(
            item["callback_data"]
            for item in buttons(claimed.edits[0])
            if item["text"] == "交给 AI"
        )
        delegated = await click(delegate, 6)
        assert "ai / respond" in delegated.edits[0]["text"]

    asyncio.run(exercise())
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_view_and_actual_base_sync_do_not_change_material_progress(conn, config):
    cfg = configured(config, base_sync=True)
    case_id, _, _ = make_turn(conn)
    stamp = "2026-09-01T00:00:00+00:00"
    conn.execute(
        "UPDATE cases SET last_material_progress_at=? WHERE case_id=?", (stamp, case_id)
    )
    case_detail(conn, case_id=case_id)
    workbench_snapshot(conn, config=cfg)

    def runner(argv):
        return CommandResult(
            {"record_id_list": ["rec_fixture"]}
            if "+record-batch-create" in argv
            else {},
            "user",
            [],
        )

    sync_case(conn, cfg, case_id=case_id, runner=runner)
    assert (
        conn.execute(
            "SELECT last_material_progress_at FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        == stamp
    )
    conn.execute(
        "UPDATE cases SET next_action='新增核对版本步骤' WHERE case_id=?", (case_id,)
    )
    assert (
        conn.execute(
            "SELECT last_material_progress_at FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        != stamp
    )


def test_operator_round_changes_preserve_busy_physical_board_lock(conn, config):
    case_id, _, _ = make_turn(conn)
    now = datetime.now(UTC).isoformat()
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    conn.execute(
        "INSERT INTO locks(lock_key,owner,case_id,scope,acquired_at,expires_at,heartbeat_at) VALUES('board1',?,?,'fixture',?,?,?)",
        (f"{case_id}:old-session", case_id, now, expires, now),
    )
    before = dict(
        conn.execute("SELECT * FROM locks WHERE lock_key='board1'").fetchone()
    )
    action(conn, case_id, "resolve")
    action(conn, case_id, "reopen")
    assert (
        dict(conn.execute("SELECT * FROM locks WHERE lock_key='board1'").fetchone())
        == before
    )
    assert "重开不会释放设备锁" in case_detail(conn, case_id=case_id)["preview"]["text"]
    approval_id, fingerprint, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="new-session",
        action=normalized_board_action(case_id, "new-session", 15),
        expires_at=expires,
    )
    with pytest.raises(ApprovalError, match="already leased"):
        decide_approval(
            conn,
            config,
            approval_id=approval_id,
            approve=True,
            approver_user_id="owner-user",
            approver_chat_id="owner-chat",
            message_id="approve-new",
            decision_text="approve",
            expected_digest=fingerprint,
        )
    assert (
        dict(conn.execute("SELECT * FROM locks WHERE lock_key='board1'").fetchone())
        == before
    )


def test_previous_round_board_evidence_is_not_current_round_validation(conn, config):
    cfg = active_config(config)
    case_id, event_pk, _ = make_turn(conn)
    old = "2000-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO evidence(evidence_id,case_id,evidence_layer,freshness_at,visibility,claim,result,created_at) VALUES('historical-board',?,'ram_boot',?,'internal','board1','prior run success',?)",
        (case_id, old, old),
    )
    action(conn, case_id, "resolve")
    action(conn, case_id, "reopen")
    control_communication(
        conn,
        case_id=case_id,
        action="delegate",
        actor_id="owner-user",
        external_id="current-delegate",
    )
    conn.execute("UPDATE cases SET state='answering' WHERE case_id=?", (case_id,))
    row_id = queue_reply(conn, cfg, case_id, event_pk)
    row = dict(
        conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (row_id,)).fetchone()
    )
    assert record_reply_delivered(
        conn,
        row=row,
        remote_message_id="om_current",
        delivered_at=datetime.now(UTC).isoformat(),
    )
    assert (
        conn.execute(
            "SELECT outcome FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        == "awaiting_validation"
    )
    assert "prior run success" in case_detail(conn, case_id=case_id)["preview"]["text"]


def test_investigation_wait_handoff_has_complete_facts_differences_and_next_steps(
    conn, config
):
    cfg = review_fixtures.active_config(config)
    case_id, job_id = review_fixtures.make_job(conn, cfg)
    bundle = prepare_codex_review(
        conn, cfg, job_id=job_id, runner=review_fixtures.remote_runner()
    )
    version = conn.execute(
        "SELECT version FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()[0]
    fact = "<verified static only>" + "长事实" * 1200

    def hermes(argv, prompt, timeout):
        return ExecutionResult(
            argv,
            0,
            json.dumps(
                {
                    "decision_id": f"hermes-review-{bundle['review_id']}",
                    "case_id": case_id,
                    "expected_case_version": version,
                    "intent": "wait",
                    "confidence": 0.8,
                    "evidence_ids": [],
                    "reply_draft": None,
                    "proposed_actions": [],
                    "facts": [fact],
                    "inferences": ["可能为环境差异"],
                    "unknowns": ["对方U-Boot版本和启动介质尚未核对"],
                }
            ),
            "",
        )

    result = run_hermes_review(
        conn,
        cfg,
        job_id=job_id,
        remote_runner=review_fixtures.remote_runner(),
        hermes_runner=hermes,
    )
    case = conn.execute(
        "SELECT state,outcome FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    assert tuple(case) == ("monitoring", "awaiting_validation")
    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM outbox WHERE outbox_id=?",
            (result["escalation_outbox_id"],),
        ).fetchone()[0]
    )
    assert (
        "事实" in payload["text"]
        and "未确认差异" in payload["text"]
        and "下一步" in payload["text"]
    )
    assert payload["buttons"][0]["text"] == "完整调查详情"
    stored = json.loads(
        conn.execute("SELECT content_json FROM case_handoffs").fetchone()[0]
    )
    assert stored["facts"] == [fact] and stored["suggested_next_action"]
    first = case_detail(conn, case_id=case_id)["preview"]
    text = "".join(
        html.unescape(
            case_detail(conn, case_id=case_id, page=page)["preview"]["text"].split(
                "\n\n", 1
            )[1]
        )
        for page in range(1, first["page_count"] + 1)
    )
    assert fact in text and "本板正常或未复现不等于对方现场已解决" in text
    assert (
        conn.execute(
            "SELECT count(*) FROM outbox WHERE channel='feishu_im'"
        ).fetchone()[0]
        == 0
    )


def test_old_completed_codex_job_cannot_notify_or_change_reopened_round(conn, config):
    cfg = review_fixtures.active_config(config)
    case_id, job_id = review_fixtures.make_job(conn, cfg)
    action(conn, case_id, "resolve")
    action(conn, case_id, "reopen")
    before = list(conn.iterdump())

    def fail(*_):
        pytest.fail("old round should not run remote checks or model")

    result = handle_codex_completion(
        conn, cfg, job_id=job_id, remote_runner=fail, hermes_runner=fail
    )
    assert result["suppressed"] and result["outbox_id"] is None
    assert list(conn.iterdump()) == before


def test_same_codex_brief_after_explicit_round_delegation_creates_new_job(conn, config):
    cfg = review_fixtures.active_config(config)
    case_id, _, _ = make_turn(conn)
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (case_id,))
    brief = "# UNTRUSTED INPUT\nfixture\n# FORBIDDEN ACTIONS\nNo push\n# ACCEPTANCE TESTS\nBuild"
    original, _ = create_codex_job(
        conn, cfg, case_id=case_id, brief=brief, repo="u-boot"
    )
    action(conn, case_id, "resolve")
    action(conn, case_id, "reopen")
    with pytest.raises(ExecutorError):
        create_codex_job(conn, cfg, case_id=case_id, brief=brief, repo="u-boot")
    control_communication(
        conn,
        case_id=case_id,
        action="delegate",
        actor_id="owner-user",
        external_id="new-codex-delegate",
    )
    current, created = create_codex_job(
        conn, cfg, case_id=case_id, brief=brief, repo="u-boot"
    )
    assert created and current != original
    assert (
        conn.execute("SELECT state FROM jobs WHERE job_id=?", (original,)).fetchone()[0]
        == "cancelled"
    )
    assert (
        conn.execute(
            "SELECT lifecycle_round FROM jobs WHERE job_id=?", (current,)
        ).fetchone()[0]
        == 2
    )
