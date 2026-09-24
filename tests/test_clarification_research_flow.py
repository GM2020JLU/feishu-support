"""Real inbound/retrieval/review/outbox flow with only injected fixture transports."""

from __future__ import annotations

import copy
import json

import pytest
from test_clarification_context import claim_ready_question, positive_review
from test_routing import active_config, route_value

from k3_support.conversation_context import admit_im_event
from k3_support.delivery import deliver_claimed
from k3_support.lark import CommandResult
from k3_support.orchestrator import complete_research_route, process_inbound
from k3_support.retrieval import run_retrieval_job
from k3_support.routing import set_requester_profile
from k3_support.store import claim_jobs
from k3_support.timeutil import iso_now


def finish_fixture_lookup(conn, cfg, job_id, *, documents=None, messages=None):
    documents = (
        documents
        if documents is not None
        else [
            {
                "title": "Boot release notes",
                "url": "https://example.feishu.cn/wiki/boot-release",
                "content": "A matching software revision is needed for regression comparison.",
            }
        ]
    )
    claimed = claim_jobs(conn, "fixture-retriever", job_types=("retrieve",))
    assert [item["job_id"] for item in claimed] == [job_id]

    def runner(argv):
        assert argv[-2:] == ["--as", "user"]
        if argv[:2] == ["drive", "+search"]:
            return CommandResult(
                {
                    "has_more": False,
                    "results": [
                        {
                            "title_highlighted": item["title"],
                            "summary_highlighted": item.get("summary", ""),
                            "result_meta": {
                                "doc_types": "DOCX",
                                "token": f"doc_{i}",
                                "url": item["url"],
                            },
                        }
                        for i, item in enumerate(documents)
                    ],
                },
                "user",
                [],
            )
        if argv[:2] == ["im", "+messages-search"]:
            return CommandResult(
                {"messages": messages or [], "has_more": False}, "user", []
            )
        assert argv[:2] == ["docs", "+fetch"]
        url = argv[argv.index("--doc") + 1]
        match = next(item for item in documents if item["url"] == url)
        return CommandResult(
            {
                "document": {
                    "document_id": "fixture-document",
                    "revision_id": 7,
                    "content": match.get("content", "fixture content"),
                }
            },
            "user",
            [],
        )

    result = run_retrieval_job(conn, cfg, job_id=job_id, runner=runner)
    assert result["job_id"] == job_id and result["followup_eligible"]
    return result


def prepared_research(conn, config):
    cfg = active_config(config, auto_faq=True, codex=True)
    event_pk, _ = admit_im_event(
        conn,
        cfg,
        {
            "source": "feishu_user_poll",
            "identity": "user",
            "external_id": "om_research_question",
            "sender_id": "ou_qa",
            "chat_id": "oc_qa",
            "occurred_at": iso_now(),
            "payload": {"chat_type": "p2p", "content": "升级到新版后启动卡住了"},
        },
    )
    set_requester_profile(
        conn, requester_id="ou_qa", relationship="peer", function_role="qa"
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="fixture-router",
        config=cfg,
        message_router=lambda _: route_value(
            "clarify",
            issue_type="bug",
            severity="P2",
            reason_codes=["missing_version"],
            clarification_question="现场复现使用的软件版本号是什么？",
            fallback_route="research",
        ),
        clarification_reviewer=lambda _: pytest.fail(
            "no lookup yet: do not question the caller"
        ),
    )
    assert not conn.execute(
        "SELECT 1 FROM outbox WHERE action_type='clarify'"
    ).fetchone()
    assert (
        conn.execute(
            "SELECT route FROM route_decisions WHERE event_pk=?", (event_pk,)
        ).fetchone()[0]
        == "research"
    )
    assert len(result["job_ids"]) == 1
    retrieval = finish_fixture_lookup(conn, cfg, result["job_ids"][0])
    return cfg, result, retrieval


def test_actual_initial_question_waits_for_lookup_then_review_and_exact_delivery(
    conn, config, monkeypatch
):
    cfg, result, retrieval = prepared_research(conn, config)
    captured = []

    def reviewer(request):
        captured.append(copy.deepcopy(request))
        return positive_review(request)

    completion = complete_research_route(
        conn,
        cfg,
        case_id=result["case_id"],
        retrieval_result=retrieval,
        selector=None,
        clarification_reviewer=reviewer,
    )
    assert completion["state"] == "clarification_queued"
    assert len(captured) == 1
    assert captured[0]["case"]["state"] == "investigating"
    assert (
        captured[0]["available_sources_and_checks"][0]["job_id"] == retrieval["job_id"]
    )
    assert (
        captured[0]["available_sources_and_checks"][0]["output_digest"]
        == retrieval["artifact_sha256"]
    )
    assert (
        conn.execute("SELECT count(*) FROM jobs WHERE job_type='codex'").fetchone()[0]
        == 0
    )
    item = conn.execute(
        "SELECT * FROM outbox WHERE outbox_id=?", (completion["outbox_id"],)
    ).fetchone()
    payload = json.loads(item["payload_json"])
    assert payload["clarification_review"]["request"] == captured[0]
    calls = []

    def fake_lark(argv):
        calls.append(argv)
        if "+chat-messages-list" in argv:
            return CommandResult({"messages": [], "has_more": False}, "user", [])
        assert argv[:2] == ["im", "+messages-reply"]
        assert payload["text"] in argv and "om_research_question" in argv
        return CommandResult({"message_id": "om_actual_small_question"}, "user", [])

    receipt = deliver_claimed(
        conn, cfg, claim_ready_question(conn, item, monkeypatch), lark_runner=fake_lark
    )
    assert receipt.remote_id == "om_actual_small_question"
    assert len([argv for argv in calls if "+messages-reply" in argv]) == 1
    assert (
        conn.execute(
            "SELECT count(*) FROM outbox WHERE action_type='clarify'"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("change", ["new_version", "supervisor", "reviewer_reject"])
def test_real_research_continuation_does_not_turn_missing_or_changed_basis_into_a_question(
    conn, config, change
):
    cfg, result, retrieval = prepared_research(conn, config)
    if change == "new_version":
        admit_im_event(
            conn,
            cfg,
            {
                "source": "feishu_user_poll",
                "identity": "user",
                "external_id": "om_version_followup",
                "sender_id": "ou_qa",
                "chat_id": "oc_qa",
                "occurred_at": iso_now(),
                "payload": {
                    "chat_type": "p2p",
                    "parent_id": "om_research_question",
                    "content": "当前软件版本是 v1.3",
                },
            },
        )
    if change == "supervisor":
        set_requester_profile(
            conn, requester_id="ou_qa", relationship="supervisor", function_role="qa"
        )
    completion = complete_research_route(
        conn,
        cfg,
        case_id=result["case_id"],
        retrieval_result=retrieval,
        selector=None,
        clarification_reviewer=(lambda _: None)
        if change == "reviewer_reject"
        else (lambda _: pytest.fail("changed input/audience must not be questioned")),
    )
    assert completion["state"] in {"stale", "continued_to_codex", "needs_owner_review"}
    assert not conn.execute(
        "SELECT 1 FROM outbox WHERE action_type='clarify'"
    ).fetchone()
    assert not conn.execute(
        "SELECT 1 FROM outbox_attempts WHERE dispatch_started_at IS NOT NULL"
    ).fetchone()
