from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_knowledge_release import signed_fixture, observed_fixture_selector

from k3_support import delivery
from k3_support.conversation_context import admit_im_event, resolve_event_context
from k3_support.coordination import (
    bind_ai_communication,
    control_communication,
    ensure_turn,
)
from k3_support.db import transaction
from k3_support.ids import digest
from k3_support.knowledge_answer import approved_answer_markdown
from k3_support.knowledge_release import bind_knowledge_reply
from k3_support.knowledge_runtime import query_knowledge
from k3_support.lark import CommandResult
from k3_support.message_format import format_feishu_ai_message
from k3_support.operations import reconcile
from k3_support.routing import record_route_decision
from k3_support.store import claim_jobs, create_case, enqueue_outbox


def case_reply(conn, config, tmp_path, monkeypatch, *, investigating=False):
    cfg, release, policy, _observed, _write = signed_fixture(
        conn, config, tmp_path, monkeypatch
    )
    cfg.raw["mode"] = "active"
    cfg.raw["features"]["auto_faq"] = True
    cfg.raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    # Fresh generation: facts originate in this caller's actual input, not a
    # label/old reply stamped after ensure_turn creates a context.
    event_pk, _ = admit_im_event(
        conn,
        cfg,
        {
            "source": "feishu_user_poll",
            "identity": "user",
            "external_id": "om_release_test",
            "payload": {
                "content": "K3 U-Boot 里怎么确认 UFS 是否识别？现场使用 K3，已经进入 U-Boot，存储介质 UFS，版本 commit-1。",
                "chat_type": "p2p",
            },
            "occurred_at": "2026-09-07T01:00:00+00:00",
            "sender_id": "ou_fixture",
            "chat_id": "oc_fixture",
        },
    )
    case_id, _ = create_case(
        conn,
        title="Synthetic FAQ",
        case_type="faq",
        severity="P3",
        confidence=0.99,
        requester_id="ou_fixture",
        requester_chat_id="oc_fixture",
        source_event_pk=event_pk,
    )
    conn.execute(
        "UPDATE cases SET state=? WHERE case_id=?",
        ("investigating" if investigating else "answering", case_id),
    )
    with transaction(conn):
        turn = ensure_turn(conn, case_id=case_id, source_event_pk=event_pk)
        binding = bind_ai_communication(
            conn, cfg, case_id=case_id, source_event_pk=event_pk
        )
    snapshot = resolve_event_context(conn, event_pk)
    observed = query_knowledge(
        conn,
        query=snapshot["query"],
        requester_id="ou_fixture",
        chat_id="oc_fixture",
        context_binding=snapshot["binding"],
        options=cfg.raw["knowledge_retrieval"],
        selector=observed_fixture_selector,
    )
    assert observed["selected_entry"] is not None, observed
    record_route_decision(
        conn,
        event_pk=event_pk,
        case_id=case_id,
        profile={},
        knowledge=observed["selected_entry"],
        route={
            "route": "direct_answer",
            "proposed_route": "direct_answer",
            "confidence": 1.0,
            "issue_type": "faq",
            "severity": "P3",
            "domain": "boot",
            "repository_hints": [],
            "reason_codes": [],
            "requires_owner_judgment": False,
            "model_output_digest": digest(observed),
        },
    )
    text = format_feishu_ai_message(
        approved_answer_markdown(conn, observed["selected_entry"])
    )
    proof = bind_knowledge_reply(
        conn,
        cfg,
        source_event_pk=event_pk,
        knowledge_ids=list(release["entries"]),
        text=text,
    )
    assert proof["release_digest"], proof
    with transaction(conn):
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="reply",
            destination="om_release_test",
            payload={
                "text": text,
                "identity": "user",
                "reply_basis": "approved_knowledge",
                "knowledge_release": proof,
            },
            idempotency_key="fresh-context-signed-reply",
            case_id=case_id,
            source_event_pk=event_pk,
            **{
                key: binding[key]
                for key in (
                    "turn_id",
                    "turn_revision",
                    "communication_fence",
                    "context_id",
                    "context_revision",
                    "context_digest",
                )
            },
        )
    monkeypatch.setattr("k3_support.ingress.poll_operator_activity", lambda *a, **k: {})
    return cfg, release, policy, outbox_id, case_id, turn["turn_id"]


def current(conn, table, key, value):
    return dict(
        conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
    )


def refuse(conn, cfg, *, reason="knowledge_release_unconfigured"):
    cfg.raw["knowledge_release"]["artifact_path"] = None
    row = delivery.claim_outbox(conn, worker_id="synthetic-sender")
    with pytest.raises(delivery.DeliverySuppressed, match=reason):
        delivery.deliver_claimed(
            conn, cfg, row, lark_runner=lambda _: pytest.fail("blocked reply sent")
        )
    return row


def test_knowledge_refusal_creates_durable_handoff_not_silent_answering(
    conn, config, tmp_path, monkeypatch
):
    cfg, _, _, oid, cid, tid = case_reply(conn, config, tmp_path, monkeypatch)
    old_turn = current(conn, "conversation_turns", "turn_id", tid)
    refuse(conn, cfg)
    turn = current(conn, "conversation_turns", "turn_id", tid)
    assert turn["state"] == "human_hold"
    assert turn["communication_owner"] == "human" and turn["fence"] > old_turn["fence"]
    case = current(conn, "cases", "case_id", cid)
    assert case["next_action"] and case["outcome"] == "unknown"
    block = current(conn, "active_delivery_blocks", "outbox_id", oid)
    assert (
        block["reason"].startswith("knowledge_release:") and not block["was_delivered"]
    )
    notice = current(conn, "outbox", "outbox_id", block["notification_outbox_id"])
    assert notice["channel"] == "telegram" and notice["action_type"] == "owner_decision"
    assert json.loads(notice["payload_json"])["control_fence"] == turn["fence"]
    assert current(conn, "outbox", "outbox_id", oid)["state"] == "cancelled"
    assert conn.execute("SELECT count(*) FROM operator_activities").fetchone()[0] == 0
    reconcile(conn, config=cfg)
    reconcile(conn, config=cfg)
    assert conn.execute("SELECT count(*) FROM delivery_blocks").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT count(*) FROM outbox WHERE action_type='owner_decision'"
        ).fetchone()[0]
        == 1
    )


def test_communication_block_preserves_independent_execution_and_board_lease(
    conn, config, tmp_path, monkeypatch
):
    cfg, _, _, _, cid, _ = case_reply(
        conn, config, tmp_path, monkeypatch, investigating=True
    )
    from k3_support.retrieval import create_retrieval_job, retrieval_input_for_case

    event_pk = conn.execute(
        "SELECT source_event_pk FROM outbox WHERE case_id=?", (cid,)
    ).fetchone()[0]
    current_input = retrieval_input_for_case(
        conn, case_id=cid, source_event_pk=event_pk
    )
    job_id, _ = create_retrieval_job(
        conn,
        cfg,
        case_id=cid,
        query=current_input["full_query"],
        source_event_pk=event_pk,
        context_binding=current_input["context_binding"],
    )
    now = datetime.now(UTC)
    conn.execute(
        """INSERT INTO locks(lock_key,owner,case_id,scope,acquired_at,expires_at,heartbeat_at)
           VALUES('board1','test-board-session',?,'board',?,?,?)""",
        (
            cid,
            now.isoformat(),
            (now + timedelta(seconds=300)).isoformat(),
            now.isoformat(),
        ),
    )
    job = current(conn, "jobs", "job_id", job_id)
    lock = current(conn, "locks", "lock_key", "board1")
    refuse(conn, cfg)
    assert current(conn, "cases", "case_id", cid)["state"] == "investigating"
    assert current(conn, "jobs", "job_id", job_id) == job
    assert current(conn, "locks", "lock_key", "board1") == lock
    assert [row["job_id"] for row in claim_jobs(conn, "independent-worker")] == [job_id]


def test_reconcile_recovers_previous_version_cancel_without_duplicate_notification(
    conn, config, tmp_path, monkeypatch
):
    cfg, _, _, oid, cid, tid = case_reply(conn, config, tmp_path, monkeypatch)
    row = delivery.claim_outbox(conn, worker_id="old-sender")
    with transaction(conn):
        conn.execute(
            "UPDATE outbox SET state='cancelled',suppression_reason='knowledge_release: expired' WHERE outbox_id=?",
            (oid,),
        )
    assert current(conn, "conversation_turns", "turn_id", tid)["state"] == "ai_sending"
    result = reconcile(conn, config=cfg)
    assert result["recovered_delivery_blocks"] == 1
    assert current(conn, "cases", "case_id", cid)["next_action"]
    assert (
        current(conn, "active_delivery_blocks", "outbox_id", oid)["claim_token"]
        == row["claim_token"]
    )
    assert reconcile(conn, config=cfg)["recovered_delivery_blocks"] == 0


@pytest.mark.parametrize("action", ["claim", "suggest_only"])
def test_late_block_does_not_override_human_communication(
    conn, config, tmp_path, monkeypatch, action
):
    from k3_support.delivery_recovery import record_blocked_delivery

    cfg, _, _, _, cid, tid = case_reply(conn, config, tmp_path, monkeypatch)
    row = delivery.claim_outbox(conn, worker_id="sender")
    control_communication(
        conn,
        case_id=cid,
        action=action,
        actor_id="owner-user",
        external_id="owner-took-over",
    )
    before_case = current(conn, "cases", "case_id", cid)
    before_turn = current(conn, "conversation_turns", "turn_id", tid)
    with transaction(conn):
        assert (
            record_blocked_delivery(conn, cfg, row, reason="knowledge_release: expired")
            is False
        )
    assert current(conn, "cases", "case_id", cid) == before_case
    assert current(conn, "conversation_turns", "turn_id", tid) == before_turn
    assert conn.execute("SELECT count(*) FROM delivery_blocks").fetchone()[0] == 0


def test_commit_failure_does_not_leave_partial_handoff_or_lost_notice(
    conn, config, tmp_path, monkeypatch
):
    from k3_support import delivery_recovery

    cfg, _, _, oid, cid, tid = case_reply(conn, config, tmp_path, monkeypatch)
    cfg.raw["knowledge_release"]["artifact_path"] = None
    row = delivery.claim_outbox(conn, worker_id="sender")
    before_turn = current(conn, "conversation_turns", "turn_id", tid)
    before_case = current(conn, "cases", "case_id", cid)
    with monkeypatch.context() as patch:
        patch.setattr(
            delivery_recovery,
            "enqueue_notice",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("synthetic enqueue crash")
            ),
        )
        with pytest.raises(RuntimeError, match="synthetic enqueue crash"):
            delivery.deliver_claimed(
                conn, cfg, row, lark_runner=lambda _: pytest.fail("must not send")
            )
    assert current(conn, "outbox", "outbox_id", oid)["state"] == "sending"
    assert current(conn, "conversation_turns", "turn_id", tid) == before_turn
    assert current(conn, "cases", "case_id", cid) == before_case
    assert conn.execute("SELECT count(*) FROM delivery_blocks").fetchone()[0] == 0
    with pytest.raises(delivery.DeliverySuppressed):
        delivery.deliver_claimed(
            conn, cfg, row, lark_runner=lambda _: pytest.fail("must not send")
        )
    assert conn.execute("SELECT count(*) FROM delivery_blocks").fetchone()[0] == 1


def test_post_dispatch_revocation_preserves_receipt_and_requests_review(
    conn, config, tmp_path, monkeypatch
):
    cfg, release, policy, oid, _, tid = case_reply(conn, config, tmp_path, monkeypatch)
    calls = []

    def send(argv):
        calls.append(argv)
        policy["revoked_release_ids"].append(release["release_id"])
        return CommandResult({"message_id": "om_already_sent"}, "user", [])

    receipt = delivery.deliver_claimed(
        conn, cfg, delivery.claim_outbox(conn, worker_id="sender"), lark_runner=send
    )
    assert receipt.remote_id == "om_already_sent" and len(calls) == 1
    saved = current(conn, "outbox", "outbox_id", oid)
    assert (
        saved["state"] == "delivered"
        and saved["remote_message_id"] == receipt.remote_id
    )
    block = current(conn, "active_delivery_blocks", "outbox_id", oid)
    assert block["was_delivered"] and "已送达" in block["next_action"]
    assert current(conn, "conversation_turns", "turn_id", tid)["state"] != "ai_sending"
    reconcile(conn, config=cfg)
    assert len(calls) == 1


def test_blocked_reply_cannot_be_revived_by_delegate_or_state_rewrite(
    conn, config, tmp_path, monkeypatch
):
    import sqlite3

    cfg, _, _, oid, cid, _ = case_reply(conn, config, tmp_path, monkeypatch)
    refuse(conn, cfg)
    control_communication(
        conn,
        case_id=cid,
        action="delegate",
        actor_id="owner-user",
        external_id="new-delegation",
    )
    assert current(conn, "outbox", "outbox_id", oid)["state"] == "cancelled"
    with pytest.raises(sqlite3.IntegrityError, match="fresh query"):
        conn.execute("UPDATE outbox SET state='pending' WHERE outbox_id=?", (oid,))
    assert (
        conn.execute("SELECT count(*) FROM active_delivery_blocks").fetchone()[0] == 0
    )


def test_regular_control_cancellation_does_not_create_delivery_block(
    conn, config, tmp_path, monkeypatch
):
    cfg, _, _, _, cid, _ = case_reply(conn, config, tmp_path, monkeypatch)
    control_communication(
        conn,
        case_id=cid,
        action="claim",
        actor_id="owner-user",
        external_id="regular-claim",
    )
    reconcile(conn, config=cfg)
    assert conn.execute("SELECT count(*) FROM delivery_blocks").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT count(*) FROM outbox WHERE action_type='owner_decision'"
        ).fetchone()[0]
        == 0
    )


def test_owner_notice_is_deferred_during_quiet_hours(
    conn, config, tmp_path, monkeypatch
):
    from k3_support import delivery_recovery

    cfg, _, _, oid, _, _ = case_reply(conn, config, tmp_path, monkeypatch)
    monkeypatch.setattr(
        delivery_recovery, "_now", lambda: datetime(2026, 9, 7, 14, 0, tzinfo=UTC)
    )
    refuse(conn, cfg)
    block = current(conn, "active_delivery_blocks", "outbox_id", oid)
    notice = current(conn, "outbox", "outbox_id", block["notification_outbox_id"])
    assert notice["not_before"] == "2026-09-08T01:00:00+00:00"


@pytest.mark.parametrize(
    "failure", ["revoked", "content_changed", "binding_missing", "dispatch_revoked"]
)
def test_real_release_gates_project_a_single_handoff(
    conn, config, tmp_path, monkeypatch, failure
):
    cfg, release, policy, oid, _, tid = case_reply(conn, config, tmp_path, monkeypatch)
    if failure == "revoked":
        policy["revoked_release_ids"].append(release["release_id"])
    elif failure == "content_changed":
        conn.execute(
            "UPDATE knowledge_entries SET answer_markdown='unreviewed replacement'"
        )
    elif failure == "binding_missing":
        saved = current(conn, "outbox", "outbox_id", oid)
        payload = json.loads(saved["payload_json"])
        payload.pop("knowledge_release")
        conn.execute(
            "UPDATE outbox SET payload_json=? WHERE outbox_id=?",
            (json.dumps(payload), oid),
        )
    else:
        begin = delivery._begin_dispatch

        def revoke_at_dispatch(*args):
            policy["revoked_release_ids"].append(release["release_id"])
            return begin(*args)

        monkeypatch.setattr(delivery, "_begin_dispatch", revoke_at_dispatch)
    with pytest.raises(delivery.DeliverySuppressed, match="knowledge_release"):
        delivery.deliver_claimed(
            conn,
            cfg,
            delivery.claim_outbox(conn, worker_id="sender"),
            lark_runner=lambda _: pytest.fail("invalid release sent"),
        )
    assert current(conn, "conversation_turns", "turn_id", tid)["state"] == "human_hold"
    assert (
        current(conn, "active_delivery_blocks", "outbox_id", oid)["was_delivered"] == 0
    )
    assert (
        conn.execute(
            "SELECT dispatch_started_at FROM outbox_attempts WHERE outbox_id=?", (oid,)
        ).fetchone()[0]
        is None
    )
    assert reconcile(conn, config=cfg)["recovered_delivery_blocks"] == 0


def test_persistent_notice_intent_survives_missing_identity_but_respects_pause(
    conn, config, tmp_path, monkeypatch
):
    from test_runtime_control import click, issue_and_bind

    cfg, _, _, oid, _, _ = case_reply(conn, config, tmp_path, monkeypatch)
    target = cfg.raw["identity"]["telegram_control_chat_id"]
    cfg.raw["identity"]["telegram_control_chat_id"] = None
    refuse(conn, cfg)
    assert (
        current(conn, "active_delivery_blocks", "outbox_id", oid)[
            "notification_outbox_id"
        ]
        is None
    )
    cfg.raw["identity"]["telegram_control_chat_id"] = target
    panel = issue_and_bind(conn, cfg)
    click(conn, cfg, panel, "global_pause")
    reconcile(conn, config=cfg)
    assert (
        current(conn, "delivery_blocks", "outbox_id", oid)["notification_outbox_id"]
        is None
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM outbox WHERE action_type='owner_decision'"
        ).fetchone()[0]
        == 0
    )


def test_persistent_notice_intent_recovers_once_after_identity_configured(
    conn, config, tmp_path, monkeypatch
):
    cfg, _, _, oid, _, _ = case_reply(conn, config, tmp_path, monkeypatch)
    target = cfg.raw["identity"]["telegram_control_chat_id"]
    cfg.raw["identity"]["telegram_control_chat_id"] = None
    refuse(conn, cfg)
    cfg.raw["identity"]["telegram_control_chat_id"] = target
    reconcile(conn, config=cfg)
    reconcile(conn, config=cfg)
    assert current(conn, "active_delivery_blocks", "outbox_id", oid)[
        "notification_outbox_id"
    ]
    assert (
        conn.execute(
            "SELECT count(*) FROM outbox WHERE action_type='owner_decision'"
        ).fetchone()[0]
        == 1
    )


def test_watchdog_distinguishes_missing_projection_from_visible_handoff(
    conn, config, tmp_path, monkeypatch
):
    from k3_support.watchdog import collect_health_alerts

    cfg, _, _, oid, _, _ = case_reply(conn, config, tmp_path, monkeypatch)
    delivery.claim_outbox(conn, worker_id="legacy-sender")
    conn.execute(
        "UPDATE outbox SET state='cancelled',suppression_reason='knowledge_release: expired' WHERE outbox_id=?",
        (oid,),
    )
    alerts = collect_health_alerts(conn, cfg)
    assert [a for a in alerts if a["key"] == "delivery_handoff_missing"]
    reconcile(conn, config=cfg)
    assert not [
        a
        for a in collect_health_alerts(conn, cfg)
        if a["key"] == "delivery_handoff_missing"
    ]


def test_owner_claim_between_notice_precheck_and_dispatch_is_quiet(
    conn, config, tmp_path, monkeypatch
):
    from k3_support import delivery_recovery

    cfg, _, _, _, cid, _ = case_reply(conn, config, tmp_path, monkeypatch)
    monkeypatch.setattr(
        delivery_recovery, "_now", lambda: datetime(2026, 9, 7, 2, 0, tzinfo=UTC)
    )
    refuse(conn, cfg)
    notice = delivery.claim_outbox(conn, worker_id="notice-sender")
    assert notice is not None and notice["channel"] == "telegram"
    begin = delivery._begin_dispatch

    def claim_before_dispatch(*args):
        control_communication(
            conn,
            case_id=cid,
            action="claim",
            actor_id="owner-user",
            external_id="claim-at-dispatch",
        )
        return begin(*args)

    monkeypatch.setattr(delivery, "_begin_dispatch", claim_before_dispatch)
    with pytest.raises(
        delivery.DeliverySuppressed, match="delivery_block_notice_superseded"
    ):
        delivery.deliver_claimed(
            conn,
            cfg,
            notice,
            telegram_button_runner=lambda *a, **k: pytest.fail("obsolete notice sent"),
        )
    assert (
        current(conn, "outbox", "outbox_id", notice["outbox_id"])["state"]
        == "cancelled"
    )
    assert (
        conn.execute(
            "SELECT dispatch_started_at FROM outbox_attempts WHERE outbox_id=?",
            (notice["outbox_id"],),
        ).fetchone()[0]
        is None
    )


def test_explicit_delegate_after_block_starts_fresh_claimable_lookup(
    conn, config, tmp_path, monkeypatch
):
    from k3_support.control import ControlMessage, execute_control

    cfg, _, _, oid, cid, _ = case_reply(conn, config, tmp_path, monkeypatch)
    refuse(conn, cfg)
    cfg.raw["features"]["codex"] = True
    result = execute_control(
        conn,
        cfg,
        ControlMessage(
            "owner-user", "owner-chat", "fresh-delegation", f"delegate {cid}"
        ),
    )
    assert result["continuation"]["created"]
    assert "新查证已排队" in current(conn, "cases", "case_id", cid)["next_action"]
    assert current(conn, "outbox", "outbox_id", oid)["state"] == "cancelled"
    assert [job["job_id"] for job in claim_jobs(conn, "fresh-research-worker")] == [
        result["continuation"]["job_id"]
    ]
