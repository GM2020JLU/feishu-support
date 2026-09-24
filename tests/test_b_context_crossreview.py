"""Independent synthetic B-path regression probes; no external transports."""

from __future__ import annotations

import pytest
from test_conversation_context import admitted, case, item, reply
from test_retrieval_context import empty_runner, make_job, setup
from test_routing import active_config, route_value

from k3_support import delivery, orchestrator
from k3_support.conversation_context import context_snapshot
from k3_support.executors import ExecutorError
from k3_support.lark import CommandResult
from k3_support.operations import reconcile
from k3_support.orchestrator import process_inbound
from k3_support.retrieval import run_retrieval_job
from k3_support.store import claim_jobs


def test_independent_p2p_topic_must_not_silently_strand_previously_queued_reply(
    conn, config
):
    cfg = active_config(config, codex=False)
    source, original = admitted(conn, cfg, item(group=False))
    case_id = case(conn, source)
    conn.execute("UPDATE cases SET state='answering' WHERE case_id=?", (case_id,))
    draft = reply(conn, cfg, source, case_id)
    conn.execute("UPDATE inbound_events SET status='processed' WHERE event_pk=?", (source,))
    next_source, _ = admitted(
        conn, cfg, item(2, group=False, content="另一个独立问题：如何更新 EC 固件？")
    )
    next_result = process_inbound(
        conn,
        event_pk=next_source,
        worker_id="synthetic-independent-topic",
        config=cfg,
        message_router=lambda _: route_value("research", conversation_relation="standalone"),
    )
    assert next_result["created"] and next_result["case_id"] != case_id
    reconcile(conn, config=cfg)
    assert conn.execute("SELECT state FROM outbox WHERE outbox_id=?", (draft["outbox_id"],)).fetchone()[0] == "cancelled"
    assert conn.execute("SELECT status FROM inbound_events WHERE event_pk=?", (source,)).fetchone()[0] == "processed"
    old = context_snapshot(conn, original["context_id"])
    assert not old["pending_associations"]
    pending_work = conn.execute(
        "SELECT count(*) FROM jobs WHERE case_id=? AND state IN ('queued','running')",
        (case_id,),
    ).fetchone()[0]
    visible_handoff = conn.execute(
        "SELECT count(*) FROM outbox WHERE case_id=? AND channel='telegram' AND state IN ('pending','retry','sending','delivered')",
        (case_id,),
    ).fetchone()[0]
    turn = conn.execute("SELECT state,communication_owner FROM conversation_turns WHERE case_id=?", (case_id,)).fetchone()
    assert pending_work or visible_handoff or turn["communication_owner"] == "human", {
        "old_context_state": old["state"],
        "old_turn": dict(turn),
        "pending_work": pending_work,
        "visible_handoff": visible_handoff,
        "old_case": dict(conn.execute("SELECT state,next_action FROM cases WHERE case_id=?", (case_id,)).fetchone()),
    }


def test_ignored_standalone_p2p_message_must_not_leave_permanent_association_hold(
    conn, config
):
    cfg = active_config(config, codex=False)
    source, original = admitted(conn, cfg, item(group=False))
    case_id = case(conn, source)
    conn.execute("UPDATE cases SET state='answering' WHERE case_id=?", (case_id,))
    reply(conn, cfg, source, case_id)
    conn.execute("UPDATE inbound_events SET status='processed' WHERE event_pk=?", (source,))
    next_source, _ = admitted(
        conn, cfg, item(2, group=False, content="另说一句，周末愉快！")
    )
    result = process_inbound(
        conn,
        event_pk=next_source,
        worker_id="synthetic-ignored-topic",
        config=cfg,
        message_router=lambda _: route_value(
            "ignore", issue_type="request", repository_hints=[],
            reason_codes=["non_work_noise"], conversation_relation="standalone",
        ),
    )
    assert result.get("ignored") and result["case_id"] is None, result
    old = context_snapshot(conn, original["context_id"])
    assert not old["pending_associations"], {
        "old_context_state": old["state"],
        "pending": old["pending_associations"],
        "new_event_status": conn.execute("SELECT status FROM inbound_events WHERE event_pk=?", (next_source,)).fetchone()[0],
    }


def test_new_input_after_retrieval_gate_but_before_codex_insert_cannot_start_stale_job(
    conn, config, monkeypatch
):
    cfg = active_config(config)
    _, case_id, snapshot = setup(conn, cfg)
    job_id = make_job(conn, cfg, case_id, snapshot)
    assert claim_jobs(conn, "synthetic-reader")[0]["job_id"] == job_id
    result = run_retrieval_job(conn, cfg, job_id=job_id, runner=empty_runner)
    original = orchestrator._retrieval_context

    def read_then_change(config, retrieval_result):
        text = original(config, retrieval_result)
        admitted(conn, cfg, item(2, parent="om_context_1", content="更正，现在是 EVB"))
        return text

    monkeypatch.setattr(orchestrator, "_retrieval_context", read_then_change)
    with pytest.raises(ExecutorError, match="retrieval input changed"):
        orchestrator.queue_codex_after_retrieval(
            conn, cfg, case_id=case_id, query=result["query"], retrieval_result=result
        )
    assert conn.execute("SELECT count(*) FROM jobs WHERE job_type='codex'").fetchone()[0] == 0


def test_new_input_during_real_ack_dispatch_keeps_receipt_without_old_projection(
    conn, config, monkeypatch
):
    cfg = active_config(config, codex=False)
    source, _ = admitted(conn, cfg, item(group=False))
    result = process_inbound(
        conn, event_pk=source, worker_id="synthetic-ack", config=cfg,
        message_router=lambda _: route_value("research"),
    )
    case_id = result["case_id"]
    conn.execute("UPDATE outbox SET not_before=NULL WHERE action_type='ack'")
    row = delivery.claim_outbox(conn, worker_id="synthetic-sender")
    assert row and row["action_type"] == "ack"
    monkeypatch.setattr("k3_support.ingress.poll_operator_activity", lambda *a, **k: {})
    calls = []

    def sender(argv):
        calls.append(argv)
        admitted(conn, cfg, item(2, group=False, parent="om_context_1", content="更正，不是 Pico，是 EVB"))
        return CommandResult({"message_id": "om_synthetic_ack_receipt"}, "user", [])

    receipt = delivery.deliver_claimed(conn, cfg, row, lark_runner=sender)
    assert receipt.remote_id == "om_synthetic_ack_receipt" and len(calls) == 1
    assert conn.execute(
        "SELECT count(*) FROM outbox_attempt_events WHERE claim_token=? AND event_type='delivered' AND remote_message_id=?",
        (row["claim_token"], receipt.remote_id),
    ).fetchone()[0] == 1
    assert conn.execute("SELECT state FROM outbox WHERE outbox_id=?", (row["outbox_id"],)).fetchone()[0] == "cancelled"
    assert conn.execute("SELECT state FROM conversation_turns WHERE case_id=?", (case_id,)).fetchone()[0] != "open"
    reconcile(conn, config=cfg)
    assert conn.execute("SELECT state FROM outbox WHERE outbox_id=?", (row["outbox_id"],)).fetchone()[0] == "cancelled"
