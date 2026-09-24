"""Bound long retrieval keeps old evidence without gaining new reply authority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_conversation_context import admitted, case, item, settings

from k3_support import conversation_context as context
from k3_support.coordination import control_communication, ensure_turn
from k3_support.db import connect
from k3_support.lark import CommandResult
from k3_support.retrieval import (
    RetrievalError,
    _search_query,
    create_retrieval_job,
    retrieval_input_for_case,
    run_retrieval_job,
    validate_retrieval_binding,
)
from k3_support.store import claim_jobs, ingest_event


def setup(conn, config):
    key, seed = admitted(conn, config, item(content="Pico 风扇如何控制"))
    cid = case(conn, key)
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (cid,))
    ensure_turn(conn, case_id=cid, source_event_pk=key)
    return key, cid, context.project_context(conn, seed["context_id"])


def make_job(conn, config, cid, snapshot, *, generation=None):
    return create_retrieval_job(
        conn,
        config,
        case_id=cid,
        source_event_pk=snapshot["focus_event_pk"],
        query=snapshot["query"],
        context_binding=snapshot["binding"],
        request_generation=generation,
    )[0]


def empty_runner(argv):
    return CommandResult({"results": [], "messages": [], "has_more": False}, "user", [])


def test_full_context_input_is_persisted_and_metadata_is_not_search_query(conn, config):
    first, cid, initial = setup(conn, config)
    second, _ = admitted(
        conn, config, item(2, parent="om_context_1", content="更正：现在是 EVB")
    )
    current = context.project_context(conn, initial["context_id"])
    job_id = make_job(conn, config, cid, current)
    job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    saved = json.loads(job["context_json"])
    assert saved["full_query"] == current["query"] and "EVB" in saved["full_query"]
    assert saved["source_event_pk"] == second and first != second
    assert saved["input_binding"]["facts_digest"] == current["facts_digest"]
    assert (
        "消息" not in saved["query"]
        and "om_" not in saved["query"]
        and "colleague" not in saved["query"]
    )
    assert len(saved["query"]) <= 30
    assert validate_retrieval_binding(conn, job_id, require_ai=True) == (True, None)


@pytest.mark.parametrize("mismatch", ["query", "source", "binding", "case"])
def test_create_job_rejects_not_current_context_input(conn, config, mismatch):
    first, cid, initial = setup(conn, config)
    second, _ = admitted(
        conn, config, item(2, parent="om_context_1", content="现在 EVB")
    )
    current = context.project_context(conn, initial["context_id"])
    args = {
        "case_id": cid,
        "source_event_pk": second,
        "query": current["query"],
        "context_binding": current["binding"],
    }
    if mismatch == "query":
        args["query"] = "a caller replacement query"
    elif mismatch == "source":
        args["source_event_pk"] = first
    elif mismatch == "binding":
        args["context_binding"] = initial["binding"]
    else:
        args["case_id"] = case(conn, second)
    with pytest.raises(RetrievalError):
        create_retrieval_job(conn, config, **args)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["new_input", "human_claim", "human_claim_delegate"])
def test_slow_retrieval_keeps_history_but_cannot_publish_or_override_owner(
    conn, config, change
):
    from k3_support.orchestrator import (
        complete_research_route,
        queue_codex_after_retrieval,
    )

    config.raw["mode"] = "active"
    config.raw["features"]["codex"] = True
    config.raw["features"]["auto_faq"] = True
    _, cid, initial = setup(conn, config)
    job_id = make_job(conn, config, cid, initial)
    assert claim_jobs(conn, "synthetic-retriever")[0]["job_id"] == job_id
    other = connect(config.database_path)
    calls = []

    def runner(argv):
        assert not conn.in_transaction
        calls.append(argv)
        if len(calls) == 1:
            if change == "new_input":
                admitted(
                    other,
                    config,
                    item(2, parent="om_context_1", content="更正，现在 EVB"),
                )
            else:
                control_communication(
                    other,
                    case_id=cid,
                    action="claim",
                    actor_id="owner-user",
                    external_id="claim-running-retrieval",
                )
                if change == "human_claim_delegate":
                    control_communication(
                        other,
                        case_id=cid,
                        action="delegate",
                        actor_id="owner-user",
                        external_id="delegate-running-retrieval",
                    )
            other.execute(
                "UPDATE cases SET next_action='owner-controlled next step' WHERE case_id=?",
                (cid,),
            )
        if argv[:2] == ["im", "+messages-search"]:
            return CommandResult(
                {
                    "messages": [
                        {
                            "message_id": "om_synthetic_evidence",
                            "content": "Synthetic historic fan evidence",
                            "chat_id": "oc_old",
                            "sender": {"id": "ou_other"},
                            "create_time": "1234567890",
                        }
                    ]
                },
                "user",
                [],
            )
        return empty_runner(argv)

    try:
        result = run_retrieval_job(conn, config, job_id=job_id, runner=runner)
    finally:
        other.close()
    assert (
        len(calls) == 2
        and not result["context_current"]
        and not result["followup_eligible"]
    )
    assert result["evidence_ids"] and Path(result["artifact_path"]).is_file()
    assert (
        json.loads(Path(result["artifact_path"]).read_text())["full_query"]
        == initial["query"]
    )
    assert (
        conn.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        == "succeeded"
    )
    assert (
        conn.execute(
            "SELECT next_action FROM cases WHERE case_id=?", (cid,)
        ).fetchone()[0]
        == "owner-controlled next step"
    )
    assert not validate_retrieval_binding(conn, job_id, require_ai=True)[0]
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    # Even forged convenient result flags do not bypass the persisted parent
    # binding at the actual continuation/reply integration entry points.
    proposed = {**result, "context_current": True, "followup_eligible": True}
    assert (
        queue_codex_after_retrieval(
            conn,
            config,
            case_id=cid,
            query="ignored replacement",
            retrieval_result=proposed,
        )
        is None
    )
    assert (
        complete_research_route(
            conn,
            config,
            case_id=cid,
            retrieval_result=proposed,
            selector=lambda _: pytest.fail("stale result reached a reply model"),
        )["state"]
        == "stale"
    )
    assert (
        conn.execute("SELECT count(*) FROM jobs WHERE job_type='codex'").fetchone()[0]
        == 0
    )


def test_fresh_delegate_input_uses_current_full_context_and_new_generation(
    conn, config
):
    _, cid, initial = setup(conn, config)
    old = make_job(conn, config, cid, initial)
    admitted(conn, config, item(2, parent="om_context_1", content="更正，现在 EVB"))
    control_communication(
        conn, case_id=cid, action="claim", actor_id="owner-user", external_id="claim"
    )
    control_communication(
        conn,
        case_id=cid,
        action="delegate",
        actor_id="owner-user",
        external_id="delegate",
    )
    current = retrieval_input_for_case(conn, case_id=cid, project=True)
    assert "Pico" in current["full_query"] and "EVB" in current["full_query"]
    new, created = create_retrieval_job(
        conn,
        config,
        case_id=cid,
        query=current["full_query"],
        source_event_pk=current["source_event_pk"],
        context_binding=current["context_binding"],
        request_generation="a" * 64,
    )
    assert (
        created
        and new != old
        and validate_retrieval_binding(conn, new, require_ai=True)[0]
    )
    assert not validate_retrieval_binding(conn, old, require_ai=True)[0]


def test_project_for_delegate_cannot_wash_pending_association(conn, config):
    settings(config)
    key, old = admitted(conn, config, item(group=False))
    cid = case(conn, key)
    context.bind_context_case(conn, old["context_id"], cid)
    admitted(conn, config, item(2, group=False))
    with pytest.raises(RetrievalError, match="awaiting_relation"):
        retrieval_input_for_case(conn, case_id=cid, project=True)


@pytest.mark.parametrize("source", ["feishu_user_poll", "timer"])
def test_legacy_followup_uses_real_source_type_not_caller_label(conn, config, source):
    value = item(group=False)
    value["source"] = source
    key, _ = ingest_event(conn, **value)
    cid = case(conn, key)
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (cid,))
    job_id, _ = create_retrieval_job(
        conn, config, case_id=cid, query="Synthetic query", source_event_pk=key
    )
    valid, reason = validate_retrieval_binding(conn, job_id, require_ai=True)
    assert valid is (source == "timer")
    if source != "timer":
        assert reason == "retrieval_context_binding_missing"
    saved = json.loads(
        conn.execute(
            "SELECT context_json FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    )
    saved["input_binding"]["kind"] = "legacy_non_im"
    conn.execute(
        "UPDATE jobs SET context_json=? WHERE job_id=?", (json.dumps(saved), job_id)
    )
    assert validate_retrieval_binding(conn, job_id, require_ai=True)[0] is (
        source == "timer"
    )


def test_same_compact_prefix_does_not_deduplicate_distinct_full_input(conn, config):
    key, _ = ingest_event(conn, **{**item(group=False), "source": "timer"})
    cid = case(conn, key)
    prefix = "a" * 40
    first, _ = create_retrieval_job(
        conn, config, case_id=cid, query=prefix + " first", source_event_pk=key
    )
    second, _ = create_retrieval_job(
        conn, config, case_id=cid, query=prefix + " second", source_event_pk=key
    )
    assert first != second and _search_query(prefix + " first") == _search_query(
        prefix + " second"
    )


@pytest.mark.parametrize(
    "field", ["query", "full_query", "raw_query_digest", "input_binding_digest"]
)
def test_changed_stored_provider_input_never_runs_or_gains_followup(
    conn, config, field
):
    _, cid, initial = setup(conn, config)
    job_id = make_job(conn, config, cid, initial)
    claim_jobs(conn, "retriever")
    saved = json.loads(
        conn.execute(
            "SELECT context_json FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    )
    saved[field] = "changed after preview"
    conn.execute(
        "UPDATE jobs SET context_json=? WHERE job_id=?", (json.dumps(saved), job_id)
    )
    assert not validate_retrieval_binding(
        conn, {"job_id": job_id, "followup_eligible": True}, require_ai=True
    )[0]
    with pytest.raises(RetrievalError, match="input digest"):
        run_retrieval_job(
            conn,
            config,
            job_id=job_id,
            runner=lambda _: pytest.fail("modified request ran"),
        )
