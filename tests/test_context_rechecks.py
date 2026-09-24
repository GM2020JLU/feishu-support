"""Synthetic independent-topic closure; no outbound transport is used."""

from __future__ import annotations

import json

import pytest
from test_conversation_context import admitted, case, item, reply
from test_routing import route_value

from k3_support import conversation_context as context
from k3_support.coordination import control_communication
from k3_support.routing import record_route_decision


def original(conn, config, *, action="reply", delivered=False, with_job=False):
    source, snapshot = admitted(conn, config, item(group=False))
    case_id = case(conn, source)
    queued = reply(conn, config, source, case_id)
    conn.execute(
        "UPDATE outbox SET action_type=?,state=? WHERE outbox_id=?",
        (action, "delivered" if delivered else "pending", queued["outbox_id"]),
    )
    if delivered:
        conn.execute(
            "UPDATE conversation_turns SET state='ai_sent' WHERE case_id=?", (case_id,)
        )
    conn.execute(
        "UPDATE cases SET state=?,next_action='prior-next-action' WHERE case_id=?",
        ("monitoring" if delivered else "answering", case_id),
    )
    if with_job:
        now = "2026-09-07T01:00:00+00:00"
        conn.execute(
            """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,created_at,updated_at)
               VALUES('independent-debug',?,'codex','running','fixture',?,?,?)""",
            (case_id, now, now, now),
        )
        conn.execute(
            """INSERT INTO locks(lock_key,owner,scope,case_id,acquired_at,heartbeat_at,expires_at)
               VALUES('board1','independent-session','board',?,?,?,?)""",
            (case_id, now, now, now),
        )
    return source, case_id, queued, snapshot


def standalone(conn, config):
    source, snapshot = admitted(
        conn, config, item(2, group=False, content="另一个独立问题")
    )
    new_case = case(conn, source)
    context.bind_context_case(conn, snapshot["context_id"], new_case)
    return source, snapshot


@pytest.mark.parametrize("action", ["reply", "clarify", "ack"])
def test_cancelled_reply_question_or_orphan_ack_gets_one_durable_handoff(
    conn, config, action
):
    _, case_id, queued, snapshot = original(conn, config, action=action)
    _, independent = standalone(conn, config)
    intents = context.pending_context_rechecks(conn)
    assert len(intents) == 1
    intent = intents[0]
    assert intent["case_id"] == case_id
    assert intent["revoked_outbox_ids"] == [queued["outbox_id"]]
    assert intent["context_binding"]["context_id"] == snapshot["context_id"]
    assert intent["turns"] and intent["turns"][0]["state"] == "human_hold"
    current = conn.execute(
        "SELECT state,owner,next_action FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    assert current["owner"] == "hermes" and current["state"] == "answering"
    assert "重新委托" in current["next_action"]
    context.resolve_pending_associations(conn, independent["context_id"])
    assert context.pending_context_rechecks(conn) == intents
    assert conn.execute("SELECT count(*) FROM delivery_blocks").fetchone()[0] == 0


def test_recheck_communication_hold_does_not_cancel_independent_debug_or_board(
    conn, config
):
    _, case_id, _, _ = original(conn, config, with_job=True)
    before_job = dict(
        conn.execute("SELECT * FROM jobs WHERE job_id='independent-debug'").fetchone()
    )
    before_lock = dict(
        conn.execute("SELECT * FROM locks WHERE lock_key='board1'").fetchone()
    )
    standalone(conn, config)
    assert context.pending_context_rechecks(conn)[0]["case_id"] == case_id
    assert (
        dict(
            conn.execute(
                "SELECT * FROM jobs WHERE job_id='independent-debug'"
            ).fetchone()
        )
        == before_job
    )
    assert (
        dict(conn.execute("SELECT * FROM locks WHERE lock_key='board1'").fetchone())
        == before_lock
    )


def test_already_delivered_faq_is_not_reopened_by_independent_chat(conn, config):
    _, case_id, queued, snapshot = original(conn, config, delivered=True)
    _, new_snapshot = standalone(conn, config)
    assert context.pending_context_rechecks(conn) == []
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE outbox_id=?", (queued["outbox_id"],)
        ).fetchone()[0]
        == "delivered"
    )
    turn = conn.execute(
        "SELECT state,communication_owner FROM conversation_turns WHERE case_id=?",
        (case_id,),
    ).fetchone()
    assert tuple(turn) == ("ai_sent", "ai")
    assert (
        context.context_snapshot(conn, snapshot["context_id"])["communication_owner"]
        == "ai"
    )
    assert not context.context_snapshot(conn, new_snapshot["context_id"])[
        "candidate_context_ids"
    ]


def test_cancelled_ack_with_running_job_remains_visible_to_human(conn, config):
    _, case_id, queued, _ = original(conn, config, action="ack", with_job=True)
    standalone(conn, config)
    assert context.pending_context_rechecks(conn)[0]["revoked_outbox_ids"] == [
        queued["outbox_id"]
    ]
    assert (
        conn.execute(
            "SELECT state FROM jobs WHERE job_id='independent-debug'"
        ).fetchone()[0]
        == "running"
    )
    assert (
        conn.execute(
            "SELECT communication_owner FROM conversation_turns WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]
        == "human"
    )


def test_cancelled_ack_retrieval_can_finish_evidence_without_losing_handoff(
    conn, config
):
    from test_retrieval_context import empty_runner, make_job

    from k3_support.retrieval import run_retrieval_job
    from k3_support.store import claim_jobs

    _, case_id, _, snapshot = original(conn, config, action="ack")
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (case_id,))
    current = context.project_context(conn, snapshot["context_id"])
    job_id = make_job(conn, config, case_id, current)
    assert claim_jobs(conn, "independent-reader")[0]["job_id"] == job_id
    standalone(conn, config)
    intent = context.pending_context_rechecks(conn)
    assert len(intent) == 1
    next_action = conn.execute(
        "SELECT next_action FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()[0]
    result = run_retrieval_job(conn, config, job_id=job_id, runner=empty_runner)
    assert not result["context_current"] and not result["followup_eligible"]
    assert result["artifact_path"]
    assert (
        conn.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        == "succeeded"
    )
    assert context.pending_context_rechecks(conn) == intent
    assert (
        conn.execute(
            "SELECT next_action FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        == next_action
    )


def test_old_cancelled_reply_is_not_mistaken_for_this_hold_cancellation(conn, config):
    _, _, queued, _ = original(conn, config)
    conn.execute(
        "UPDATE outbox SET state='cancelled',suppression_reason='conversation_context_changed' WHERE outbox_id=?",
        (queued["outbox_id"],),
    )
    standalone(conn, config)
    assert context.pending_context_rechecks(conn) == []


def test_pending_notice_loses_eligibility_after_explicit_redelegate(conn, config):
    _, case_id, _, _ = original(conn, config)
    standalone(conn, config)
    assert len(context.pending_context_rechecks(conn)) == 1
    control_communication(
        conn,
        case_id=case_id,
        action="delegate",
        actor_id="owner-user",
        external_id="synthetic-redelegate",
    )
    assert context.pending_context_rechecks(conn) == []
    assert (
        conn.execute(
            "SELECT count(*) FROM case_events WHERE event_type='context_recheck_required'"
        ).fetchone()[0]
        == 1
    )


def ignore_route(conn, event_pk, **changes):
    snapshot = context.resolve_event_context(conn, event_pk)
    context.project_context(conn, snapshot["context_id"])
    decision = route_value("ignore", reason_codes=["non_work_noise"], **changes)
    decision.update(proposed_route="ignore", model_output_digest="synthetic-route-only")
    record_route_decision(
        conn, event_pk=event_pk, case_id=None, route=decision, profile={}
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"confidence": 0.79},
        {"conversation_relation": "continuation"},
        {"requires_owner_judgment": True},
    ],
)
def test_untrusted_ignore_decision_cannot_wash_pending_holds(conn, config, changes):
    _, _, _, snapshot = original(conn, config)
    source, _ = admitted(conn, config, item(2, group=False, content="周末愉快"))
    ignore_route(conn, source, **changes)
    with pytest.raises(context.ContextError, match="not verified"):
        context.release_independent_event(conn, source)
    assert context.context_snapshot(conn, snapshot["context_id"])[
        "pending_associations"
    ]
    assert context.pending_context_rechecks(conn) == []


def test_verified_ignore_releases_holds_and_duplicate_does_not_create_them_again(
    conn, config
):
    _, _, _, snapshot = original(conn, config)
    value = item(2, group=False, content="周末愉快")
    source, new_snapshot = admitted(conn, config, value)
    ignore_route(conn, source)
    assert context.release_independent_event(conn, source) == [snapshot["context_id"]]
    assert context.release_independent_event(conn, source) == []
    assert context.admit_im_event(conn, config, value) == (source, False)
    assert not context.context_snapshot(conn, snapshot["context_id"])[
        "pending_associations"
    ]
    assert not context.context_snapshot(conn, new_snapshot["context_id"])[
        "candidate_context_ids"
    ]
    row = conn.execute(
        "SELECT detail_json FROM case_events WHERE event_type='context_recheck_required'"
    ).fetchone()
    assert json.loads(row[0])["notification_intent"]


def test_new_anchored_input_after_ignore_route_requires_fresh_relation(conn, config):
    _, _, _, snapshot = original(conn, config)
    source, _ = admitted(conn, config, item(2, group=False, content="周末愉快"))
    ignore_route(conn, source)
    admitted(
        conn,
        config,
        item(
            3,
            group=False,
            parent="om_context_2",
            content="补充一下，实际上还是刚才的风扇问题",
        ),
    )
    with pytest.raises(context.ContextError, match="not verified"):
        context.release_independent_event(conn, source)
    assert context.context_snapshot(conn, snapshot["context_id"])[
        "pending_associations"
    ]
    assert context.pending_context_rechecks(conn) == []
