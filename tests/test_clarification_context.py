"""Synthetic contextual review; no real model, mail, board or message transport."""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from test_routing import route_value

from k3_support.routing import (
    choose_route,
    clarification_allowed,
    set_requester_profile,
)


def scenario(
    conn,
    config,
    content="升级到新版后启动卡住了",
    *,
    research=True,
    documents=None,
    messages=None,
):
    from test_clarification_research_flow import finish_fixture_lookup

    from k3_support.conversation_context import (
        admit_im_event,
        bind_context_case,
        resolve_event_context,
    )
    from k3_support.retrieval import create_retrieval_job
    from k3_support.store import create_case
    from k3_support.timeutil import iso_now

    event_pk, _ = admit_im_event(
        conn,
        config,
        {
            "source": "feishu_user_poll",
            "identity": "user",
            "external_id": "om_clarification",
            "chat_id": "oc_qa",
            "sender_id": "ou_qa",
            "thread_id": None,
            "payload": {"content": content, "chat_type": "p2p"},
            "occurred_at": iso_now(),
        },
    )
    case_id = create_case(
        conn, title="boot question", case_type="bug", severity="P2", confidence=0.9
    )[0]
    context = bind_context_case(
        conn, resolve_event_context(conn, event_pk)["context_id"], case_id
    )
    profile = set_requester_profile(
        conn, requester_id="ou_qa", relationship="peer", function_role="qa"
    )
    route = route_value(
        "clarify",
        issue_type="bug",
        reason_codes=["missing_version"],
        clarification_question="现场复现使用的软件版本号是什么？",
        fallback_route="research",
    )
    if research:
        conn.execute("UPDATE cases SET state='triage' WHERE case_id=?", (case_id,))
        job_id, _ = create_retrieval_job(
            conn,
            config,
            case_id=case_id,
            query=context["query"],
            source_event_pk=event_pk,
            context_binding=context["binding"],
        )
        finish_fixture_lookup(
            conn, config, job_id, documents=documents, messages=messages
        )
    return case_id, context, profile, route


def positive_review(request):
    return {
        "decision": "send",
        "confidence": 0.98,
        "reason_code": "necessary_and_minimal",
        "gap": "software_version",
        "source_quote": {
            "event_pk": request["messages"][0]["event_pk"],
            "quote": request["messages"][0]["content"],
        },
        "research_refs": [request["available_sources_and_checks"][0]["ref"]],
        "impact": {
            "operation": "select_firmware_revision",
            "if_answered": "Compare changes against the caller's exact firmware release.",
            "if_unanswered": "Continue reading generic upgrade documentation without claiming a matching revision.",
            "reason": "The observed regression follows an upgrade; current lookup metadata does not identify the caller's installed version.",
        },
        "retrievability": "not_in_available_records",
    }


def allows(conn, scenario_value, *, reviewer=positive_review, record=None):
    case_id, context, profile, route = scenario_value
    return clarification_allowed(
        conn,
        case_id=case_id,
        context_binding=context["binding"],
        profile=profile,
        route=route,
        reviewer=reviewer,
        minimum_confidence=0.92,
        review_record_out=record,
    )


def test_legacy_second_opinion_without_original_context_cannot_authorize_question(conn):
    from k3_support.store import create_case

    case_id = create_case(
        conn, title="boot", case_type="bug", severity="P2", confidence=0.9
    )[0]
    profile = set_requester_profile(
        conn, requester_id="qa", relationship="peer", function_role="qa"
    )
    assert not clarification_allowed(
        conn,
        case_id=case_id,
        profile=profile,
        route=route_value(
            "clarify",
            reason_codes=["missing_version"],
            clarification_question="软件版本号是什么？",
            fallback_route="research",
        ),
        reviewer=lambda _: {
            "decision": "send",
            "confidence": 0.99,
            "reason_code": "necessary_and_minimal",
        },
        minimum_confidence=0.92,
    )


@pytest.mark.parametrize(
    "answer", [None, "malformed", {**route_value("research"), "confidence": 0.3}]
)
def test_uncertain_semantic_relation_does_not_default_to_continuation(answer):
    from k3_support.routing import unknown_profile

    result = choose_route(
        query="另一个现象",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline={"type": "bug", "severity": "P2", "confidence": 0.9},
        profile=unknown_profile("qa"),
        knowledge=None,
        router=lambda _: answer,
        minimum_confidence=0.8,
        conversation_context={
            "requires_semantic_relation": True,
            "recent_case_id": "K3-fixture",
        },
    )
    assert result["conversation_relation"] == "standalone"
    assert result["route"] == "owner_decision"


def test_full_context_positive_review_is_readonly_and_can_be_rechecked(conn, config):
    from k3_support.clarification_context import validate_clarification_review

    value = scenario(conn, config)
    before = list(conn.iterdump())
    captured, record = [], {}

    def reviewer(request):
        captured.append(copy.deepcopy(request))
        return positive_review(request)

    assert allows(conn, value, reviewer=reviewer, record=record)
    request = captured[0]
    assert request["original_problem"] == "升级到新版后启动卡住了"
    assert (
        request["known_information"]["fields"]["software_version"]["state"] == "unknown"
    )
    assert request["already_asked"] == []
    assert (
        request["available_sources_and_checks"][0]["documents"][0]["title"]
        == "Boot release notes"
    )
    assert request["rules"]["unknown_retrievability_is_not_unretrievable"]
    assert validate_clarification_review(
        conn,
        case_id=value[0],
        question=value[3]["clarification_question"],
        record=record,
    )
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize(
    "content",
    ["软件版本是v1.2，升级后启动卡住了", "当前版本是v1.3，不是v1.2，启动卡住了"],
)
def test_supplied_or_corrected_version_is_not_asked_again(conn, config, content):
    value = scenario(conn, config, content)
    assert not allows(
        conn,
        value,
        reviewer=lambda _: pytest.fail(
            "already provided version must reject before model"
        ),
    )


def test_no_completed_current_lookup_does_not_claim_the_answer_is_unretrievable(
    conn, config
):
    value = scenario(conn, config, research=False)
    assert not allows(
        conn, value, reviewer=lambda _: pytest.fail("research before bothering caller")
    )


@pytest.mark.parametrize("change", ["legacy_hash_only", "same_input_handback"])
def test_old_lookup_cannot_be_reused_after_the_current_authority_binding_changes(
    conn, config, change
):
    from k3_support.conversation_context import project_context, set_case_communication

    value = scenario(conn, config)
    assert allows(conn, value)
    if change == "legacy_hash_only":
        conn.execute(
            """UPDATE jobs SET context_json=json_remove(context_json,
            '$.input_binding','$.input_binding_digest') WHERE job_type='retrieve'"""
        )
    else:
        case_id, context, profile, route = value
        set_case_communication(
            conn, case_id, owner="human", mode="silent", reason="fixture claim"
        )
        set_case_communication(
            conn, case_id, owner="ai", mode="respond", reason="fixture recheck"
        )
        refreshed = project_context(conn, context["context_id"])
        assert refreshed["query"] == context["query"]
        assert refreshed["focus_event_pk"] == context["focus_event_pk"]
        assert refreshed["binding"] != context["binding"]
        value = case_id, refreshed, profile, route
    assert not allows(
        conn,
        value,
        reviewer=lambda _: pytest.fail("old lookup is not a new review basis"),
    )


@pytest.mark.parametrize("source", ["document", "message"])
def test_reviewer_receives_the_retrieved_body_not_just_metadata_and_can_avoid_repeat_question(
    conn, config, source
):
    body = (
        "本次同一问题已记录：提问者现场当前软件版本是 v1.3，后续应检查该版本启动日志。"
    )
    value = scenario(
        conn,
        config,
        documents=[
            {
                "title": "Boot release notes",
                "url": "https://example.feishu.cn/wiki/record",
                "content": body,
            }
        ]
        if source == "document"
        else [],
        messages=[
            {
                "message_id": "om_prior_version",
                "chat_id": "oc_qa",
                "chat_type": "p2p",
                "content": body,
                "sender": {"id": "ou_qa", "name": "Fixture QA"},
            }
        ]
        if source == "message"
        else [],
    )
    seen = []

    def reviewer(request):
        seen.append(request)
        assert (
            request["available_sources_and_checks"][0][
                "documents" if source == "document" else "messages"
            ][0]["content"]
            == body
        )
        assert (
            "not whole documents"
            in request["available_sources_and_checks"][0]["coverage"]
        )
        # This fixture is a semantic rejection, not a claim that a real model
        # has been evaluated or that arbitrary history proves the current site.
        return {**positive_review(request), "decision": "reject"}

    assert not allows(conn, value, reviewer=reviewer)
    assert len(seen) == 1
    assert not conn.execute(
        "SELECT 1 FROM outbox WHERE action_type='clarify'"
    ).fetchone()


@pytest.mark.parametrize(
    "change",
    [
        "missing_file",
        "symlink",
        "parent_symlink",
        "wrong_hash",
        "missing_content",
        "wrong_scope",
        "unread_page",
        "unread_document",
        "fetch_error",
        "truncated_body",
        "oversize",
    ],
)
def test_incomplete_or_changed_retrieval_material_never_uses_metadata_as_a_question_basis(
    conn, config, change
):
    import hashlib
    from pathlib import Path

    from k3_support.ids import canonical_json

    value = scenario(conn, config)
    assert allows(conn, value)
    job = conn.execute("SELECT * FROM jobs WHERE job_type='retrieve'").fetchone()
    path = Path(job["workdir"]) / "retrieval.json"
    if change == "missing_file":
        path.unlink()
    elif change == "symlink":
        backup = path.with_name("fixture-original.json")
        path.rename(backup)
        path.symlink_to(backup)
    elif change == "parent_symlink":
        backup = path.parent.with_name(path.parent.name + "-fixture-original")
        path.parent.rename(backup)
        path.parent.symlink_to(backup, target_is_directory=True)
    else:
        artifact = json.loads(path.read_text())
        if change in {"wrong_hash", "oversize"}:
            path.write_text("{}" if change == "wrong_hash" else "x" * 131073)
        else:
            if change == "missing_content":
                del artifact["documents"][0]["content"]
            elif change == "wrong_scope":
                artifact["input_binding"]["source_event_pk"] = "in_other_context"
            elif change == "unread_page":
                artifact["has_more"] = True
            elif change == "unread_document":
                artifact["search_count"] += 1
            elif change == "fetch_error":
                artifact["fetch_errors"] = [
                    {"source": "message_search", "error_type": "authorization"}
                ]
            elif change == "truncated_body":
                artifact["documents"][0]["content"] = "x" * 20000
            raw = canonical_json(artifact)
            path.write_text(raw)
            artifact_hash = hashlib.sha256(raw.encode()).hexdigest()
            conn.execute(
                "UPDATE jobs SET output_digest=? WHERE job_id=?",
                (artifact_hash, job["job_id"]),
            )
            conn.execute(
                "UPDATE case_suggestions SET content_json=json_set(content_json,'$.artifact_sha256',?) WHERE kind='next_action'",
                (artifact_hash,),
            )
    assert not allows(
        conn,
        value,
        reviewer=lambda _: pytest.fail("inspect actual complete material first"),
    )


@pytest.mark.parametrize(
    "change",
    [
        "question",
        "case_version",
        "new_input",
        "takeover",
        "collection",
        "projection",
        "source",
        "research",
        "profile",
        "scope",
    ],
)
def test_review_is_invalid_after_relevant_facts_or_authority_change(
    conn, config, change
):
    from k3_support.clarification_context import validate_clarification_review
    from k3_support.conversation_context import admit_im_event, set_case_communication
    from k3_support.timeutil import iso_now

    value = scenario(conn, config)
    record = {}
    assert allows(conn, value, record=record)
    case_id, _context, _, route = value
    question = route["clarification_question"]
    if change == "question":
        question = "请提供全部代码"
    if change == "case_version":
        conn.execute("UPDATE cases SET version=version+1 WHERE case_id=?", (case_id,))
    if change == "takeover":
        set_case_communication(
            conn, case_id, owner="human", mode="silent", reason="fixture"
        )
    if change == "collection":
        conn.execute("UPDATE conversation_contexts SET collection_complete=0")
    if change == "projection":
        conn.execute("UPDATE conversation_contexts SET facts_digest='bad'")
    if change == "source":
        conn.execute(
            "UPDATE inbound_events SET payload_json=?",
            (json.dumps({"content": "我已经提供版本v1.2"}),),
        )
    if change == "research":
        conn.execute("UPDATE jobs SET state='failed' WHERE job_type='retrieve'")
    if change == "scope":
        conn.execute("UPDATE jobs SET context_json='{}' WHERE job_type='retrieve'")
    if change == "profile":
        set_requester_profile(
            conn, requester_id="ou_qa", relationship="supervisor", function_role="qa"
        )
    if change == "new_input":
        admit_im_event(
            conn,
            config,
            {
                "source": "feishu_user_poll",
                "identity": "user",
                "external_id": "om_new_version",
                "chat_id": "oc_qa",
                "sender_id": "ou_qa",
                "thread_id": None,
                "payload": {
                    "content": "当前版本是v1.2",
                    "chat_type": "p2p",
                    "parent_id": "om_clarification",
                },
                "occurred_at": iso_now(),
            },
        )
    before = list(conn.iterdump())
    assert not validate_clarification_review(
        conn, case_id=case_id, question=question, record=record
    )
    assert list(conn.iterdump()) == before


def test_new_input_during_second_opinion_prevents_old_question(conn, config):
    value = scenario(conn, config)

    def reviewer(request):
        conn.execute("UPDATE conversation_contexts SET revision=revision+1")
        return positive_review(request)

    record = {"old": "must clear"}
    assert not allows(conn, value, reviewer=reviewer, record=record)
    assert record == {}


@pytest.mark.parametrize(
    "change",
    [
        "legacy",
        "nan",
        "confidence",
        "quoted_invention",
        "unknown_reference",
        "generic",
        "same_step",
        "wrong_gap",
        "retrievable",
        "unknown",
    ],
)
def test_incomplete_or_invented_model_rationale_does_not_authorize_a_question(
    conn, config, change
):
    value = scenario(conn, config)

    def reviewer(request):
        result = positive_review(request)
        if change == "legacy":
            return {
                key: result[key] for key in ("decision", "confidence", "reason_code")
            }
        if change == "nan":
            result["confidence"] = float("nan")
        if change == "confidence":
            result["confidence"] = 2
        if change == "quoted_invention":
            result["source_quote"]["quote"] = "not in the source message"
        if change == "unknown_reference":
            result["research_refs"] = ["research:invented"]
        if change == "generic":
            result["impact"]["reason"] = "help debug"
        if change == "same_step":
            result["impact"]["if_unanswered"] = result["impact"]["if_answered"]
        if change == "wrong_gap":
            result["gap"] = "board"
        if change in {"retrievable", "unknown"}:
            result["retrievability"] = change
        return result

    assert not allows(conn, value, reviewer=reviewer)


def test_second_opinion_cannot_mutate_the_original_question_by_reference(conn, config):
    value = scenario(conn, config)
    original = copy.deepcopy(value[3])

    def reviewer(request):
        answer = positive_review(request)
        request["route"]["clarification_question"] = "给我全部代码"
        return answer

    record = {}
    assert allows(conn, value, reviewer=reviewer, record=record)
    assert (
        value[3] == original
        and record["request"]["question"] == original["clarification_question"]
    )


def test_one_question_budget_is_not_reset_after_a_reply_or_new_round(conn, config):
    from k3_support.store import enqueue_outbox

    value = scenario(conn, config)
    assert allows(conn, value)
    enqueue_outbox(
        conn,
        channel="feishu_im",
        action_type="clarify",
        destination="om_clarification",
        payload={"text": "already asked", "identity": "user"},
        case_id=value[0],
        idempotency_key="clarify-once-fixture",
    )
    assert not allows(conn, value)


def test_real_hermes_prompt_receives_context_and_strict_rationale_schema_without_external_call(
    conn, config, monkeypatch
):
    from k3_support.semantic import hermes_clarification_reviewer

    value = scenario(conn, config)
    captured = []

    def runner(argv, **kwargs):
        assert argv[-1] == "--support-json-stdin"
        envelope = json.loads(kwargs["input"])
        assert envelope["protocol"] == 1
        prompt = envelope["prompt"]
        assert prompt not in argv
        captured.append(prompt)
        request = json.loads(prompt.split("REVIEW_INPUT_JSON:\n", 1)[1])
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(positive_review(request)), ""
        )

    monkeypatch.setattr("k3_support.semantic.subprocess.run", runner)
    assert allows(conn, value, reviewer=hermes_clarification_reviewer)
    assert "source_quote" in captured[0] and "升级到新版后启动卡住了" in captured[0]
    assert "not proof" in captured[0] and "Boot release notes" in captured[0]


@pytest.mark.parametrize(
    "content",
    [
        "board1版本是v9.0，我升级到新版后启动卡住了",
        "当前不是版本v1.2，升级后启动卡住了",
    ],
)
def test_test_board_and_negated_versions_are_not_claimed_as_the_callers_current_version(
    conn, config, content
):
    value = scenario(conn, config, content)
    record = {}
    assert allows(conn, value, record=record)
    facts = record["request"]["known_information"]
    assert facts["fields"]["software_version"]["state"] == "unknown"
    assert (
        any(mention["subject"] == "test_board:board1" for mention in facts["mentions"])
        if "board1" in content
        else facts["fields"]["software_version"]["excluded_values"]
    )


def test_operator_question_is_not_repeated_after_explicit_handback(conn, config):
    from test_routing import active_config

    from k3_support.clarification_context import (
        ClarificationContextError,
        build_review_context,
    )
    from k3_support.conversation_context import (
        admit_im_event,
        project_context,
        set_case_communication,
    )
    from k3_support.timeutil import iso_now

    cfg = active_config(config)
    case_id, context, _profile, route = scenario(conn, cfg)
    admit_im_event(
        conn,
        cfg,
        {
            "source": "feishu_user_poll",
            "identity": "user",
            "external_id": "om_operator_asked",
            "chat_id": "oc_qa",
            "sender_id": "ou_owner",
            "thread_id": None,
            "payload": {
                "content": "请提供当前的软件版本号？",
                "chat_type": "p2p",
                "parent_id": "om_clarification",
            },
            "occurred_at": iso_now(),
        },
    )
    set_case_communication(
        conn, case_id, owner="ai", mode="respond", reason="explicit fixture handback"
    )
    current = project_context(conn, context["context_id"])
    with pytest.raises(ClarificationContextError, match="operator already requested"):
        build_review_context(
            conn,
            case_id=case_id,
            context_binding=current["binding"],
            question=route["clarification_question"],
            route=route,
        )


def queued_question(conn, config, *, state="triage"):
    from test_routing import active_config

    from k3_support.coordination import ensure_turn
    from k3_support.orchestrator import _send_clarification

    cfg = active_config(config, auto_faq=False, codex=False)
    value = scenario(conn, cfg)
    case_id, context, _, route = value
    event = conn.execute(
        "SELECT * FROM inbound_events WHERE event_pk=?", (context["focus_event_pk"],)
    ).fetchone()
    ensure_turn(conn, case_id=case_id, source_event_pk=event["event_pk"])
    conn.execute("UPDATE cases SET state=? WHERE case_id=?", (state, case_id))
    record = {}
    assert allows(conn, value, record=record)
    outbox_id = _send_clarification(
        conn,
        case_id=case_id,
        event=event,
        worker_id="synthetic-worker",
        config=cfg,
        question=route["clarification_question"],
        review_record=record,
    )
    assert outbox_id is not None
    return (
        cfg,
        conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone(),
        record,
    )


def claim_ready_question(conn, item, monkeypatch):
    """Advance the sender's clock past the real grace; never relax its gate."""
    from k3_support.delivery import claim_outbox

    due = datetime.fromisoformat(item["not_before"]) + timedelta(seconds=1)

    class DeliveryClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return due.astimezone(tz) if tz else due.replace(tzinfo=None)

    monkeypatch.setattr("k3_support.delivery.datetime", DeliveryClock)
    claimed = claim_outbox(
        conn,
        worker_id="fixture-delivery",
        eligible=lambda row: row["outbox_id"] == item["outbox_id"],
    )
    assert claimed is not None and claimed["outbox_id"] == item["outbox_id"]
    return claimed


@pytest.mark.parametrize("state", ["triage", "investigating"])
def test_real_enqueue_projection_is_accepted_without_rewriting_historical_review(
    conn, config, monkeypatch, state
):
    from k3_support.clarification_context import (
        validate_clarification_delivery,
        validate_clarification_review,
    )
    from k3_support.delivery import deliver_claimed
    from k3_support.lark import CommandResult

    cfg, item, record = queued_question(conn, config, state=state)
    original = copy.deepcopy(record)
    assert not validate_clarification_review(
        conn,
        case_id=item["case_id"],
        question=record["request"]["question"],
        record=record,
    )
    assert validate_clarification_delivery(conn, item=item)
    assert record == original
    calls = []

    def fake_lark(argv):
        calls.append(argv)
        return CommandResult(
            {"messages": [], "has_more": False}
            if "+chat-messages-list" in argv
            else {"message_id": "om_clarify_sent"},
            "user",
            [],
        )

    claimed = claim_ready_question(conn, item, monkeypatch)
    receipt = deliver_claimed(conn, cfg, claimed, lark_runner=fake_lark)
    assert receipt.remote_id == "om_clarify_sent" and calls
    assert record == original


@pytest.mark.parametrize(
    "change",
    [
        "other_question",
        "text",
        "destination",
        "identity",
        "version",
        "event_digest",
        "event_version",
        "source",
        "profile",
        "research",
    ],
)
def test_send_projection_exempts_only_the_exact_own_question(conn, config, change):
    from k3_support.clarification_context import validate_clarification_delivery
    from k3_support.store import enqueue_outbox

    _cfg, item, _record = queued_question(conn, config)
    if change == "other_question":
        enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="clarify",
            destination="om_clarification",
            payload={"text": "another question"},
            case_id=item["case_id"],
            idempotency_key="other-question",
        )
    if change == "text":
        conn.execute(
            "UPDATE outbox SET payload_json=json_set(payload_json,'$.text','altered question') WHERE outbox_id=?",
            (item["outbox_id"],),
        )
    if change == "destination":
        conn.execute(
            "UPDATE outbox SET destination='om_other' WHERE outbox_id=?",
            (item["outbox_id"],),
        )
    if change == "identity":
        conn.execute(
            "UPDATE outbox SET payload_json=json_set(payload_json,'$.identity','bot') WHERE outbox_id=?",
            (item["outbox_id"],),
        )
    if change == "version":
        conn.execute(
            "UPDATE cases SET version=version+1 WHERE case_id=?", (item["case_id"],)
        )
    if change == "event_digest":
        conn.execute(
            "UPDATE case_events SET detail_json=json_set(detail_json,'$.review_digest','forged') WHERE event_type='clarification_requested'"
        )
    if change == "event_version":
        conn.execute(
            "UPDATE case_events SET detail_json=json_set(detail_json,'$.case_version_before',999) WHERE event_type='clarification_requested'"
        )
    if change == "source":
        conn.execute(
            "UPDATE inbound_events SET payload_json=?",
            (json.dumps({"content": "now known"}),),
        )
    if change == "profile":
        set_requester_profile(
            conn, requester_id="ou_qa", relationship="supervisor", function_role="qa"
        )
    if change == "research":
        conn.execute("UPDATE jobs SET state='failed' WHERE job_type='retrieve'")
    item = conn.execute(
        "SELECT * FROM outbox WHERE outbox_id=?", (item["outbox_id"],)
    ).fetchone()
    assert not validate_clarification_delivery(conn, item=item)


@pytest.mark.parametrize(
    "field,value",
    [("route", []), ("messages", [None]), ("case", []), ("context_binding", [])],
)
def test_malformed_stored_review_fails_closed_without_raising(
    conn, config, field, value
):
    from k3_support.clarification_context import validate_clarification_delivery
    from k3_support.ids import canonical_json, digest

    _cfg, item, _record = queued_question(conn, config)
    payload = json.loads(item["payload_json"])
    record = payload["clarification_review"]
    record["request"][field] = value
    record["record_digest"] = digest(
        {key: value for key, value in record.items() if key != "record_digest"}
    )
    conn.execute(
        "UPDATE outbox SET payload_json=? WHERE outbox_id=?",
        (canonical_json(payload), item["outbox_id"]),
    )
    conn.execute(
        "UPDATE case_events SET detail_json=json_set(detail_json,'$.review_digest',?) WHERE event_type='clarification_requested'",
        (record["record_digest"],),
    )
    current = conn.execute(
        "SELECT * FROM outbox WHERE outbox_id=?", (item["outbox_id"],)
    ).fetchone()
    assert not validate_clarification_delivery(conn, item=current)


@pytest.mark.parametrize("change", ["profile", "research", "artifact"])
@pytest.mark.parametrize("phase", ["claimed", "dispatch", "receipt"])
def test_changed_review_becomes_an_actionable_handoff_without_unjustified_question(
    conn, config, monkeypatch, change, phase
):
    from k3_support import delivery
    from k3_support.clarification_context import validate_clarification_delivery
    from k3_support.delivery_recovery import reconcile_delivery_blocks
    from k3_support.lark import CommandResult

    cfg, item, _record = queued_question(conn, config)
    assert validate_clarification_delivery(conn, item=item)
    claimed = claim_ready_question(conn, item, monkeypatch)
    assert validate_clarification_delivery(conn, item=claimed)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,
        created_at,updated_at,context_json)
        VALUES('job_independent_execution',?,'codex','queued','independent',?,?,?,'{}')""",
        (item["case_id"], now, now, now),
    )
    original_execution = dict(
        conn.execute(
            "SELECT * FROM jobs WHERE job_id='job_independent_execution'"
        ).fetchone()
    )
    calls = []

    def change_justification():
        if change == "profile":
            set_requester_profile(
                conn,
                requester_id="ou_qa",
                relationship="supervisor",
                function_role="qa",
            )
        elif change == "research":
            conn.execute("UPDATE jobs SET state='failed' WHERE job_type='retrieve'")
        else:
            from pathlib import Path

            workdir = conn.execute(
                "SELECT workdir FROM jobs WHERE job_type='retrieve'"
            ).fetchone()[0]
            artifact = Path(workdir) / "retrieval.json"
            artifact.write_bytes(artifact.read_bytes() + b"\n")

    def fake_lark(argv):
        calls.append(argv)
        if "+chat-messages-list" in argv:
            return CommandResult({"messages": [], "has_more": False}, "user", [])
        assert phase == "receipt", (
            "a no-longer-justified question reached the transport"
        )
        change_justification()
        return CommandResult({"message_id": "om_clarify_sent"}, "user", [])

    if phase == "claimed":
        change_justification()
    elif phase == "dispatch":
        actual_begin_dispatch = delivery._begin_dispatch

        def changed_before_dispatch(*args):
            change_justification()
            return actual_begin_dispatch(*args)

        monkeypatch.setattr(delivery, "_begin_dispatch", changed_before_dispatch)

    if phase == "receipt":
        receipt = delivery.deliver_claimed(conn, cfg, claimed, lark_runner=fake_lark)
        assert receipt.remote_id == "om_clarify_sent"
    else:
        with pytest.raises(delivery.DeliverySuppressed, match="clarification_review:"):
            delivery.deliver_claimed(conn, cfg, claimed, lark_runner=fake_lark)

    sent = [argv for argv in calls if "+chat-messages-list" not in argv]
    assert len(sent) == (1 if phase == "receipt" else 0)
    block = conn.execute(
        "SELECT * FROM active_delivery_blocks WHERE outbox_id=?", (item["outbox_id"],)
    ).fetchone()
    assert block and block["reason"].startswith("clarification_review:")
    assert block["was_delivered"] == (phase == "receipt")
    assert block["next_action"] and block["notification_outbox_id"]
    case = conn.execute(
        "SELECT * FROM cases WHERE case_id=?", (item["case_id"],)
    ).fetchone()
    assert (
        case["state"] == "investigating" and case["next_action"] == block["next_action"]
    )
    assert ("已送达" if phase == "receipt" else "不等待同事回答") in case["next_action"]
    turn = conn.execute(
        "SELECT * FROM conversation_turns WHERE turn_id=?", (item["turn_id"],)
    ).fetchone()
    assert turn["state"] == "human_hold" and turn["communication_owner"] == "human"
    persisted = conn.execute(
        "SELECT * FROM outbox WHERE outbox_id=?", (item["outbox_id"],)
    ).fetchone()
    assert persisted["state"] == ("delivered" if phase == "receipt" else "cancelled")
    assert persisted["remote_message_id"] == (
        "om_clarify_sent" if phase == "receipt" else None
    )
    attempt = conn.execute(
        "SELECT * FROM outbox_attempts WHERE claim_token=?", (claimed["claim_token"],)
    ).fetchone()
    assert (attempt["dispatch_started_at"] is not None) == (phase == "receipt")
    assert (
        dict(
            conn.execute(
                "SELECT * FROM jobs WHERE job_id='job_independent_execution'"
            ).fetchone()
        )
        == original_execution
    )
    reconcile_delivery_blocks(conn, cfg)
    reconcile_delivery_blocks(conn, cfg)
    assert conn.execute("SELECT count(*) FROM delivery_blocks").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT count(*) FROM outbox WHERE action_type='owner_decision'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM outbox WHERE action_type='clarify'"
        ).fetchone()[0]
        == 1
    )
