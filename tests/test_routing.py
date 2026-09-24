from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from k3_support.config import Config, validate_config
from k3_support.knowledge import create_candidate, review
from k3_support.lark import CommandResult, LarkError
from k3_support.orchestrator import (
    classify,
    complete_research_route,
    continue_research_with_codex,
    process_inbound,
)
from k3_support.routing import (
    RoutingError,
    audience_strategy,
    choose_route,
    clarification_allowed,
    latest_route,
    record_route_decision,
    refresh_requester_profile,
    review_route,
    set_requester_profile,
)
from k3_support.store import create_case, enqueue_outbox, ingest_event


def active_config(config, *, auto_faq: bool = True, codex: bool = True) -> Config:
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    raw["features"]["auto_faq"] = auto_faq
    raw["features"]["codex"] = codex
    raw["scope"]["auto_reply_chat_ids"] = ["oc_support"]
    raw["repositories"] = {
        "u-boot": {
            "path": "/data/home2/operator/WorkSpace/k3/uboot-2022.10",
            "remote": "origin",
            "base_branch": "main",
        }
    }
    return Config(validate_config(raw), config.path)


def route_value(route: str, **overrides):
    value = {
        "route": route,
        "confidence": 0.96,
        "issue_type": "investigation",
        "severity": "P3",
        "domain": "bootloader",
        "repository_hints": ["u-boot"],
        "reason_codes": ["technical_investigation"],
        "clarification_question": None,
        "fallback_route": None,
        "requires_owner_judgment": False,
        "conversation_relation": "standalone",
    }
    value.update(overrides)
    return value


def test_ai_route_understands_bug_without_literal_bug_keyword(config):
    baseline = classify(
        {"content": "升级后偶尔卡在 Starting kernel"}, "feishu_user_poll"
    )
    assert baseline["type"] == "investigation"
    decision = choose_route(
        query="升级后偶尔卡在 Starting kernel",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline=baseline,
        profile={
            "requester_id": "ou_peer",
            "relationship": "peer",
            "function_role": "engineering",
            "relationship_confidence": 1.0,
            "function_confidence": 1.0,
            "source": "operator",
            "display_name": None,
            "department": None,
            "job_title": None,
            "verified_at": None,
        },
        knowledge=None,
        router=lambda _: route_value(
            "codex_debug",
            issue_type="bug",
            severity="P2",
        ),
        minimum_confidence=0.8,
    )
    assert decision["route"] == "codex_debug"
    assert decision["issue_type"] == "bug"


def test_high_confidence_context_update_does_not_disturb_owner(config):
    decision = choose_route(
        query="里面是 OpenHarmony，ectool 在 data 下，可以烧成 Bianbu 方便测试",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline={"type": "request", "severity": "P3", "confidence": 0.45},
        profile={
            "requester_id": "ou_x",
            "relationship": "unknown",
            "function_role": "unknown",
            "relationship_confidence": 0.0,
            "function_confidence": 0.0,
            "source": "unknown",
            "display_name": None,
            "department": None,
            "job_title": None,
            "verified_at": None,
        },
        knowledge=None,
        router=lambda _: route_value(
            "ignore",
            issue_type="request",
            repository_hints=[],
            reason_codes=["context_update"],
        ),
        minimum_confidence=0.8,
    )
    assert decision["route"] == "ignore"


def test_direct_answer_without_approved_knowledge_falls_back_to_research(config):
    decision = choose_route(
        query="如何启动",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline={"type": "faq", "severity": "P3", "confidence": 0.55},
        profile={
            "requester_id": "ou_x",
            "relationship": "unknown",
            "function_role": "unknown",
            "relationship_confidence": 0.0,
            "function_confidence": 0.0,
            "source": "unknown",
            "display_name": None,
            "department": None,
            "job_title": None,
            "verified_at": None,
        },
        knowledge=None,
        router=lambda _: route_value(
            "direct_answer",
            issue_type="faq",
            reason_codes=["approved_knowledge_match"],
        ),
        minimum_confidence=0.8,
    )
    assert decision["proposed_route"] == "direct_answer"
    assert decision["route"] == "research"


def test_supervisor_is_never_automatically_asked_a_clarifying_question(config):
    profile = {
        "requester_id": "ou_boss",
        "relationship": "supervisor",
        "function_role": "management",
        "relationship_confidence": 1.0,
        "function_confidence": 0.9,
        "source": "feishu_contact",
        "display_name": "Boss",
        "department": "研发",
        "job_title": "总监",
        "verified_at": "2026-09-02T10:00:00+08:00",
    }
    decision = choose_route(
        query="这个启动问题看一下",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline={"type": "investigation", "severity": "P3", "confidence": 0.35},
        profile=profile,
        knowledge=None,
        router=lambda _: route_value(
            "clarify",
            reason_codes=["missing_logs"],
            clarification_question="请把所有日志、代码和环境信息都发给我？",
            fallback_route="codex_debug",
        ),
        minimum_confidence=0.8,
    )
    assert decision["route"] == "codex_debug"
    assert decision["clarification_question"] is None
    assert audience_strategy(profile)["auto_clarify"] is False


def test_product_or_project_commitment_routes_to_owner(config):
    profile = {
        "requester_id": "ou_pm",
        "relationship": "cross_function",
        "function_role": "project_manager",
        "relationship_confidence": 0.8,
        "function_confidence": 0.95,
        "source": "operator",
        "display_name": None,
        "department": None,
        "job_title": None,
        "verified_at": None,
    }
    decision = choose_route(
        query="这个 UFS 问题什么时候能交付？",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline={"type": "faq", "severity": "P3", "confidence": 0.55},
        profile=profile,
        knowledge=None,
        router=lambda _: route_value("research", issue_type="request"),
        minimum_confidence=0.8,
    )
    assert decision["route"] == "owner_decision"


def test_owner_judgment_hard_rule_wins_over_low_model_confidence(config):
    profile = {
        "requester_id": "ou_peer",
        "relationship": "peer",
        "function_role": "engineering",
        "relationship_confidence": 1.0,
        "function_confidence": 1.0,
        "source": "operator",
        "display_name": None,
        "department": None,
        "job_title": None,
        "verified_at": None,
    }
    decision = choose_route(
        query="这个变更是否可以承诺明天交付？",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline={"type": "request", "severity": "P3", "confidence": 0.55},
        profile=profile,
        knowledge=None,
        router=lambda _: route_value(
            "owner_decision",
            confidence=0.60,
            requires_owner_judgment=True,
            reason_codes=["requires_commitment"],
        ),
        minimum_confidence=0.8,
    )
    assert decision["route"] == "owner_decision"
    assert decision["requires_owner_judgment"] is True


def test_long_technical_answer_to_supervisor_needs_owner_judgment(config):
    profile = {
        "requester_id": "ou_boss",
        "relationship": "supervisor",
        "function_role": "management",
        "relationship_confidence": 1.0,
        "function_confidence": 0.9,
        "source": "feishu_contact",
        "display_name": "Boss",
        "department": "研发",
        "job_title": "总监",
        "verified_at": "2026-09-02T10:00:00+08:00",
    }
    knowledge = {
        "knowledge_id": "kn_long",
        "title": "Long runbook",
        "confidence": 0.98,
        "source_authority": 1.0,
        "semantic_match_confidence": 0.98,
        "answer_markdown": "```shell\n" + "x" * 700 + "\n```",
    }
    decision = choose_route(
        query="这个怎么操作？",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline={"type": "faq", "severity": "P3", "confidence": 0.55},
        profile=profile,
        knowledge=knowledge,
        router=lambda _: route_value(
            "direct_answer",
            issue_type="faq",
            reason_codes=["approved_knowledge_match"],
        ),
        minimum_confidence=0.8,
    )
    assert decision["route"] == "owner_decision"
    assert decision["requires_owner_judgment"] is True


@pytest.mark.parametrize("disclosure_class", ["internal", "team", "private"])
def test_external_requester_cannot_receive_non_public_knowledge_directly(
    disclosure_class,
):
    profile = {
        "requester_id": "ou_external",
        "relationship": "external",
        "function_role": "engineering",
        "relationship_confidence": 1.0,
        "function_confidence": 0.9,
        "source": "feishu_contact",
        "display_name": "External engineer",
        "department": None,
        "job_title": None,
        "verified_at": "2026-09-03T10:00:00+08:00",
    }
    knowledge = {
        "knowledge_id": "kn_internal",
        "title": "Internal runbook",
        "confidence": 0.98,
        "source_authority": 1.0,
        "semantic_match_confidence": 0.98,
        "answer_markdown": "内部操作步骤",
        "disclosure_class": disclosure_class,
    }
    decision = choose_route(
        query="这个怎么操作？",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline={"type": "faq", "severity": "P3", "confidence": 0.9},
        profile=profile,
        knowledge=knowledge,
        router=lambda _: route_value(
            "direct_answer",
            issue_type="faq",
            reason_codes=["approved_knowledge_match"],
        ),
        minimum_confidence=0.8,
    )
    assert decision["route"] == "owner_decision"
    assert decision["requires_owner_judgment"] is True


def test_external_requester_can_receive_public_knowledge_directly():
    profile = {
        "requester_id": "ou_external",
        "relationship": "external",
        "function_role": "engineering",
        "relationship_confidence": 1.0,
        "function_confidence": 0.9,
        "source": "feishu_contact",
        "display_name": "External engineer",
        "department": None,
        "job_title": None,
        "verified_at": "2026-09-03T10:00:00+08:00",
    }
    knowledge = {
        "knowledge_id": "kn_public",
        "title": "Public guide",
        "confidence": 0.98,
        "source_authority": 1.0,
        "semantic_match_confidence": 0.98,
        "answer_markdown": "公开操作步骤",
        "disclosure_class": "public",
    }
    decision = choose_route(
        query="这个怎么操作？",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline={"type": "faq", "severity": "P3", "confidence": 0.9},
        profile=profile,
        knowledge=knowledge,
        router=lambda _: route_value(
            "direct_answer",
            issue_type="faq",
            reason_codes=["approved_knowledge_match"],
        ),
        minimum_confidence=0.8,
    )
    assert decision["route"] == "direct_answer"


@pytest.mark.parametrize("proposed", ["research", "codex_debug", "clarify"])
def test_external_requester_cannot_bypass_owner_via_indirect_route(proposed):
    profile = {
        "requester_id": "ou_external",
        "relationship": "external",
        "function_role": "engineering",
        "relationship_confidence": 1.0,
        "function_confidence": 0.9,
        "source": "feishu_contact",
        "display_name": "外部联系人",
        "department": None,
        "job_title": None,
        "verified_at": "2026-09-03T00:00:00+00:00",
    }
    route_extra = (
        {"clarification_question": "请提供版本", "fallback_route": "research"}
        if proposed == "clarify"
        else {}
    )
    decision = choose_route(
        query="请发我内部调试文档",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline={"type": "request", "severity": "P3", "confidence": 0.9},
        profile=profile,
        knowledge=None,
        router=lambda _: route_value(
            proposed, reason_codes=["source_lookup_needed"], **route_extra
        ),
        minimum_confidence=0.8,
    )
    assert decision["route"] == "owner_decision"
    assert decision["requires_owner_judgment"] is True


def test_clarification_needs_low_risk_profile_second_review_and_is_one_shot(
    conn, config
):
    from test_clarification_context import positive_review, scenario

    case_id, context, profile, route = scenario(conn, config)
    assert clarification_allowed(
        conn,
        case_id=case_id,
        route=route,
        profile=profile,
        reviewer=positive_review,
        minimum_confidence=0.92,
        context_binding=context["binding"],
    )
    enqueue_outbox(
        conn,
        channel="feishu_im",
        action_type="clarify",
        destination="om_1",
        payload={"text": "question", "identity": "user"},
        idempotency_key="clarify-once",
        case_id=case_id,
    )
    assert not clarification_allowed(
        conn,
        case_id=case_id,
        route=route,
        profile=profile,
        reviewer=positive_review,
        minimum_confidence=0.92,
        context_binding=context["binding"],
    )


def test_low_confidence_derived_profile_cannot_be_auto_questioned(conn):
    profile = set_requester_profile(
        conn,
        requester_id="ou_probably_peer",
        relationship="cross_function",
        function_role="engineering",
        source="derived",
        relationship_confidence=0.55,
        function_confidence=0.8,
    )
    case_id, _ = create_case(
        conn,
        title="ambiguous issue",
        case_type="investigation",
        severity="P3",
        confidence=0.8,
    )
    route = {
        **route_value(
            "clarify",
            reason_codes=["missing_target"],
            clarification_question="目标板型是什么？",
            fallback_route="codex_debug",
        ),
        "proposed_route": "clarify",
        "model_output_digest": "b" * 64,
    }
    assert not clarification_allowed(
        conn,
        case_id=case_id,
        route=route,
        profile=profile,
        reviewer=lambda _: {
            "decision": "send",
            "confidence": 0.99,
            "reason_code": "necessary_and_minimal",
        },
        minimum_confidence=0.92,
    )


def test_contact_profile_uses_leader_ids_and_manual_override_wins(conn, config):
    cfg = active_config(config)
    cfg.raw["routing"]["org_profile_lookup"] = True
    operator_id = cfg.raw["identity"]["feishu_owner_open_id"]

    def runner(argv, **_):
        user_id = argv[2].rsplit("/", 1)[1]
        if user_id == "ou_boss":
            user = {
                "open_id": "ou_boss",
                "name": "Boss",
                "leader_user_id": "ou_ceo",
                "job_title": "研发总监",
                "department_ids": ["od_rnd"],
            }
        else:
            assert user_id == operator_id
            user = {
                "open_id": operator_id,
                "leader_user_id": "ou_boss",
                "department_ids": ["od_rnd"],
            }
        return CommandResult({"user": user}, "user", [])

    profile = refresh_requester_profile(
        conn, cfg, requester_id="ou_boss", runner=runner
    )
    assert profile["relationship"] == "supervisor"
    assert profile["function_role"] == "management"

    set_requester_profile(
        conn,
        requester_id="ou_boss",
        relationship="peer",
        function_role="engineering",
        source="operator",
    )
    assert (
        refresh_requester_profile(
            conn,
            cfg,
            requester_id="ou_boss",
            runner=lambda *_: (_ for _ in ()).throw(AssertionError()),
        )["relationship"]
        == "peer"
    )


def test_contact_profile_falls_back_to_user_visible_search(conn, config):
    cfg = active_config(config)
    cfg.raw["routing"]["org_profile_lookup"] = True

    def runner(argv, **_):
        if argv[0] == "api":
            raise LarkError("detailed contact scope unavailable")
        assert argv[:4] == ["contact", "+search-user", "--user-ids", "ou_colleague"]
        return CommandResult(
            {
                "users": [
                    {
                        "open_id": "ou_colleague",
                        "localized_name": "王翱",
                        "department": "应用1部",
                        "is_cross_tenant": False,
                    }
                ]
            },
            "user",
            [],
        )

    profile = refresh_requester_profile(
        conn, cfg, requester_id="ou_colleague", runner=runner
    )
    assert profile["display_name"] == "王翱"
    assert profile["department"] == "应用1部"
    assert profile["relationship"] == "unknown"
    assert profile["source"] == "feishu_contact"


def test_active_owner_decision_notifies_operator_without_replying_requester(
    conn, config
):
    cfg = active_config(config)
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_pm_eta",
        payload={"content": "这个功能什么时候可以承诺交付？", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_pm",
        chat_id="oc_pm",
    )
    set_requester_profile(
        conn,
        requester_id="ou_pm",
        relationship="cross_function",
        function_role="product_manager",
        display_name="产品同事",
        department="产品部",
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=cfg,
        message_router=lambda _: route_value(
            "owner_decision",
            issue_type="request",
            reason_codes=["requires_commitment"],
            requires_owner_judgment=True,
        ),
    )
    assert result["route"]["route"] == "owner_decision"
    row = conn.execute("SELECT channel,action_type FROM outbox").fetchone()
    assert tuple(row) == ("telegram", "owner_decision")
    payload = json.loads(conn.execute("SELECT payload_json FROM outbox").fetchone()[0])
    assert payload["parse_mode"] == "HTML"
    assert "<b>需要你判断的 K3 支持消息</b>" in payload["text"]
    assert "产品同事（跨团队同事 · 产品经理 · 产品部）" in payload["text"]
    assert "可能涉及承诺或排期" in payload["text"]
    assert "unknown" not in payload["text"]
    assert (
        conn.execute(
            "SELECT state FROM cases WHERE case_id=?", (result["case_id"],)
        ).fetchone()[0]
        == "escalated"
    )


def test_owner_decision_uses_ingress_sender_name_without_contact_scope(conn, config):
    cfg = active_config(config)
    cfg.raw["scope"]["technical_chat_ids"] = ["oc_team"]
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_named_requester",
        payload={
            "content": "这个优先级需要你决定",
            "chat_type": "group",
            "sender_name": "测试同学",
            "chat_name": "K3 技术支持",
            "mentions": [{"open_id": "ou_owner"}],
        },
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_named",
        chat_id="oc_team",
    )

    process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=cfg,
        message_router=lambda _: route_value(
            "owner_decision",
            issue_type="request",
            reason_codes=["requires_priority_decision"],
            requires_owner_judgment=True,
        ),
    )

    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM outbox WHERE action_type='owner_decision'"
        ).fetchone()[0]
    )
    assert "测试同学 · K3 技术支持" in payload["text"]
    assert "unknown" not in payload["text"]


def test_active_first_contact_without_prior_lookup_does_not_auto_question(conn, config):
    cfg = active_config(config)
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_qa_version",
        payload={"content": "升级后启动卡住了", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_qa",
        chat_id="oc_qa",
    )
    set_requester_profile(
        conn,
        requester_id="ou_qa",
        relationship="peer",
        function_role="qa",
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=cfg,
        message_router=lambda _: route_value(
            "clarify",
            issue_type="bug",
            severity="P2",
            reason_codes=["missing_version"],
            clarification_question="复现使用的软件版本号是什么？",
            fallback_route="codex_debug",
        ),
        clarification_reviewer=lambda _: {
            "decision": "send",
            "confidence": 0.97,
            "reason_code": "necessary_and_minimal",
        },
    )
    assert not conn.execute(
        "SELECT 1 FROM outbox WHERE action_type='clarify'"
    ).fetchone()
    assert (
        conn.execute(
            "SELECT route FROM route_decisions WHERE event_pk=?", (event_pk,)
        ).fetchone()[0]
        == "codex_debug"
    )
    assert result["job_ids"]


def test_research_route_replies_with_exact_selected_links_only(conn, config):
    from test_clarification_research_flow import finish_fixture_lookup

    cfg = active_config(config)
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_fan_doc",
        payload={"content": "pico 风扇怎么调", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_peer",
        chat_id="oc_peer",
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=cfg,
        message_router=lambda _: route_value(
            "research",
            issue_type="faq",
            reason_codes=["source_lookup_needed"],
        ),
    )
    selected = "https://example.feishu.cn/wiki/fan-guide"
    retrieval = finish_fixture_lookup(
        conn,
        cfg,
        result["job_ids"][0],
        documents=[
            {
                "title": "K3 Pico 风扇调节",
                "url": selected,
                "content": "private debug notebook content must not be copied",
            },
            {"title": "Unrelated", "url": "https://example.feishu.cn/wiki/unrelated"},
        ],
    )
    completion = complete_research_route(
        conn,
        cfg,
        case_id=result["case_id"],
        retrieval_result=retrieval,
        selector=lambda _: {"document_urls": [selected], "confidence": 0.97},
    )
    assert completion["state"] == "needs_owner_review"
    assert completion["reason"] == "document_route_not_evaluated"
    assert (
        conn.execute(
            "SELECT count(*) FROM outbox WHERE action_type='reply'"
        ).fetchone()[0]
        == 0
    )
    row = conn.execute(
        "SELECT channel,payload_json FROM outbox WHERE outbox_id=?",
        (completion["outbox_id"],),
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert row["channel"] == "telegram"
    assert f"[K3 Pico 风扇调节]({selected})" in payload["text"]
    assert "private debug notebook" not in payload["text"]


def test_research_failure_continues_to_codex_instead_of_stalling(conn, config):
    from k3_support.store import claim_jobs

    cfg = active_config(config)
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_research_failure",
        payload={"content": "启动介质怎么切换", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_peer",
        chat_id="oc_peer",
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=cfg,
        message_router=lambda _: route_value(
            "research",
            issue_type="faq",
            reason_codes=["source_lookup_needed"],
        ),
    )
    claim_jobs(conn, "fixture-research-failure", job_types=("retrieve",))
    continuation = continue_research_with_codex(
        conn,
        cfg,
        case_id=result["case_id"],
        query="启动介质怎么切换",
        retrieval_result=None,
        retrieval_error="LarkError",
        retrieval_job_id=result["job_ids"][0],
    )
    assert continuation is not None
    assert latest_route(conn, result["case_id"])["route"] == "codex_debug"
    assert (
        conn.execute(
            "SELECT count(*) FROM jobs WHERE case_id=? AND job_type='codex'",
            (result["case_id"],),
        ).fetchone()[0]
        == 1
    )

    reviewed = review_route(
        conn,
        route_decision_id=result["route_decision_id"],
        decision="accepted",
        reviewer_id="telegram-owner",
        note="correct route",
    )
    assert reviewed["changed"] is True
    assert (
        review_route(
            conn,
            route_decision_id=result["route_decision_id"],
            decision="accepted",
            reviewer_id="telegram-owner",
        )["changed"]
        is False
    )
    with pytest.raises(RoutingError, match="already reviewed differently"):
        review_route(
            conn,
            route_decision_id=result["route_decision_id"],
            decision="rejected",
            reviewer_id="telegram-owner",
        )


def test_high_confidence_standalone_noise_creates_no_case_or_job(conn, config):
    cfg = active_config(config)
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_noise",
        payload={"content": "今晚聚餐吗", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_peer",
        chat_id="oc_peer",
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=cfg,
        message_router=lambda _: route_value(
            "ignore",
            reason_codes=["non_work_noise"],
            repository_hints=[],
            conversation_relation="standalone",
        ),
    )

    assert result["ignored"] is True
    assert result["case_id"] is None
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    route = conn.execute(
        "SELECT case_id,route,conversation_relation FROM route_decisions"
    ).fetchone()
    assert tuple(route) == (None, "ignore", "standalone")


def test_context_update_attaches_after_routing_without_retrieval(conn, config):
    cfg = active_config(config)
    messages = (
        ("om_issue", "U-Boot 启动失败"),
        ("om_context", "补充一下，是从 UFS 启动"),
    )
    event_ids = []
    for external_id, content in messages:
        event_pk, _ = ingest_event(
            conn,
            source="feishu_user_poll",
            identity="user",
            external_id=external_id,
            payload={"content": content, "chat_type": "p2p"},
            occurred_at=datetime.now(UTC).isoformat(),
            sender_id="ou_peer",
            chat_id="oc_peer",
        )
        event_ids.append(event_pk)

    first = process_inbound(
        conn,
        event_pk=event_ids[0],
        worker_id="worker-1",
        config=cfg,
        message_router=lambda _: route_value(
            "codex_debug", conversation_relation="standalone"
        ),
    )
    jobs_before = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    second = process_inbound(
        conn,
        event_pk=event_ids[1],
        worker_id="worker-1",
        config=cfg,
        message_router=lambda request: route_value(
            "ignore",
            reason_codes=["context_update"],
            repository_hints=[],
            conversation_relation=(
                "continuation" if request["recent_conversation"] else "standalone"
            ),
        ),
    )

    assert second["case_id"] == first["case_id"]
    assert second["merged_followup"] is True
    assert second["job_ids"] == []
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == jobs_before
    route = conn.execute(
        "SELECT route,conversation_relation FROM route_decisions WHERE event_pk=?",
        (event_ids[1],),
    ).fetchone()
    assert tuple(route) == ("ignore", "continuation")


def test_ai_new_topic_starts_new_case_even_inside_followup_window(conn, config):
    cfg = active_config(config)
    case_ids = []
    for sequence, content in enumerate(("U-Boot 启动失败", "Pico 风扇怎么调"), start=1):
        event_pk, _ = ingest_event(
            conn,
            source="feishu_user_poll",
            identity="user",
            external_id=f"om_ai_topic_{sequence}",
            payload={"content": content, "chat_type": "p2p"},
            occurred_at=datetime.now(UTC).isoformat(),
            sender_id="ou_peer",
            chat_id="oc_peer",
        )
        result = process_inbound(
            conn,
            event_pk=event_pk,
            worker_id="worker-1",
            config=cfg,
            message_router=lambda request: route_value(
                "codex_debug",
                conversation_relation=(
                    "new_topic" if request["recent_conversation"] else "standalone"
                ),
            ),
        )
        case_ids.append(result["case_id"])

    assert len(set(case_ids)) == 2
    assert conn.execute("SELECT count(*) FROM route_decisions").fetchone()[0] == 2


def test_actionable_continuation_has_route_before_retrieval(conn, config):
    cfg = active_config(config)
    results = []
    for sequence, content in enumerate(
        ("U-Boot 启动失败", "补充一下，只在冷启动复现"), start=1
    ):
        event_pk, _ = ingest_event(
            conn,
            source="feishu_user_poll",
            identity="user",
            external_id=f"om_actionable_followup_{sequence}",
            payload={"content": content, "chat_type": "p2p"},
            occurred_at=datetime.now(UTC).isoformat(),
            sender_id="ou_peer",
            chat_id="oc_peer",
        )
        results.append(
            process_inbound(
                conn,
                event_pk=event_pk,
                worker_id="worker-1",
                config=cfg,
                message_router=lambda request: route_value(
                    "codex_debug",
                    conversation_relation=(
                        "continuation"
                        if request["recent_conversation"]
                        else "standalone"
                    ),
                ),
            )
        )

    assert results[1]["case_id"] == results[0]["case_id"]
    assert results[1]["merged_followup"] is True
    assert len(results[1]["job_ids"]) == 1
    assert conn.execute("SELECT count(*) FROM route_decisions").fetchone()[0] == 2
    assert (
        conn.execute("SELECT count(*) FROM jobs WHERE job_type='retrieve'").fetchone()[
            0
        ]
        == 2
    )
    assert (
        conn.execute(
            """SELECT count(*) FROM jobs j
           JOIN route_decisions r
             ON r.event_pk=json_extract(j.context_json,'$.source_event_pk')
           WHERE j.job_type='retrieve'"""
        ).fetchone()[0]
        == 2
    )


def test_retry_reuses_persisted_route_instead_of_calling_model_again(conn, config):
    cfg = active_config(config)
    first_event, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_before_crash",
        payload={"content": "U-Boot 启动失败", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_peer",
        chat_id="oc_peer",
    )
    first = process_inbound(
        conn,
        event_pk=first_event,
        worker_id="worker-1",
        config=cfg,
        message_router=lambda _: route_value(
            "codex_debug", conversation_relation="standalone"
        ),
    )
    retry_event, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_after_crash",
        payload={"content": "Pico 风扇怎么调", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_peer",
        chat_id="oc_peer",
    )
    profile = {
        "requester_id": "ou_peer",
        "relationship": "peer",
        "function_role": "engineering",
        "relationship_confidence": 1.0,
        "function_confidence": 1.0,
        "source": "operator",
        "display_name": None,
        "department": None,
        "job_title": None,
        "verified_at": None,
    }
    route = choose_route(
        query="Pico 风扇怎么调",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline={"type": "faq", "severity": "P3", "confidence": 0.55},
        profile=profile,
        knowledge=None,
        router=lambda _: route_value(
            "research",
            issue_type="faq",
            reason_codes=["source_lookup_needed"],
            conversation_relation="new_topic",
        ),
        minimum_confidence=0.8,
        conversation_context={"recent_case_id": first["case_id"]},
    )
    record_route_decision(
        conn,
        event_pk=retry_event,
        case_id=None,
        route=route,
        profile=profile,
        conversation_case_id=first["case_id"],
    )

    def must_not_run(_):
        raise AssertionError("persisted route must be reused")

    resumed = process_inbound(
        conn,
        event_pk=retry_event,
        worker_id="worker-1",
        config=cfg,
        message_router=must_not_run,
    )

    assert resumed["case_id"] != first["case_id"]
    assert resumed["merged_followup"] is False
    assert resumed["route"]["conversation_relation"] == "new_topic"
    stored = conn.execute(
        "SELECT case_id,conversation_case_id FROM route_decisions WHERE event_pk=?",
        (retry_event,),
    ).fetchone()
    assert tuple(stored) == (resumed["case_id"], first["case_id"])


def _persist_direct_answer_route(conn, event_pk: str) -> str:
    from k3_support.knowledge_corpus import build
    from k3_support.knowledge_runtime import query_knowledge

    set_requester_profile(
        conn,
        requester_id="ou_peer",
        relationship="peer",
        function_role="engineering",
        source="operator",
        relationship_confidence=1.0,
        function_confidence=1.0,
    )
    knowledge_id = create_candidate(
        conn,
        title="K3 Fastboot",
        questions=["K3 怎么进入 fastboot"],
        answer_markdown="参考已审核的 Fastboot 文档。",
        project="K3",
        module="bootloader",
        software_version=None,
        disclosure_class="internal",
        confidence=0.97,
        source_authority=0.96,
        canonical_case_id=None,
        source_digest="fastboot-source-v1",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    assert build(conn)['built']
    knowledge = query_knowledge(
        conn,
        query="K3 怎么进入 fastboot",
        requester_id="ou_peer",
        chat_id="oc_support",
        selector=lambda query, catalog: {'knowledge_id': knowledge_id, 'confidence': .99},
    )["selected_entry"]
    profile = {
        "requester_id": "ou_peer",
        "relationship": "peer",
        "function_role": "engineering",
        "relationship_confidence": 1.0,
        "function_confidence": 1.0,
        "source": "operator",
        "display_name": None,
        "department": None,
        "job_title": None,
        "verified_at": None,
    }
    route = choose_route(
        query="K3 怎么进入 fastboot",
        source="feishu_user_poll",
        chat_type="p2p",
        baseline={"type": "faq", "severity": "P3", "confidence": 0.55},
        profile=profile,
        knowledge=knowledge,
        router=lambda _: route_value(
            "direct_answer",
            issue_type="faq",
            reason_codes=["approved_knowledge_match"],
            repository_hints=[],
            conversation_relation="standalone",
        ),
        minimum_confidence=0.8,
    )
    record_route_decision(
        conn,
        event_pk=event_pk,
        case_id=None,
        route=route,
        profile=profile,
        knowledge=knowledge,
    )
    return knowledge_id


def test_direct_answer_resume_reuses_exact_persisted_knowledge(conn, config):
    cfg = active_config(config)
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_direct_resume",
        payload={"content": "K3 怎么进入 fastboot", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_peer",
        chat_id="oc_support",
    )
    knowledge_id = _persist_direct_answer_route(conn, event_pk)

    def must_not_run(*_):
        raise AssertionError("persisted semantic decisions must be reused")

    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=cfg,
        semantic_selector=must_not_run,
        message_router=must_not_run,
    )

    assert result["route"]["route"] == "direct_answer"
    assert len(result["outbox_ids"]) == 1
    stored = conn.execute(
        "SELECT knowledge_id,knowledge_source_digest FROM route_decisions"
    ).fetchone()
    assert tuple(stored) == (knowledge_id, "fastboot-source-v1")
    provenance = result["route"]["knowledge_provenance"]
    assert set(provenance) == {
        "knowledge_runtime_binding",
        "knowledge_query_digest",
        "knowledge_observed_scope",
        "knowledge_scope_facts",
        "knowledge_claim_ids",
        "knowledge_entry_fingerprint",
        "knowledge_requester_id",
        "knowledge_chat_id",
        "knowledge_profile_digest",
        "knowledge_event_digest",
    }
    assert "answer_markdown" not in json.dumps(provenance)


def test_legacy_route_without_runtime_provenance_requires_new_research(conn, config):
    cfg = active_config(config)
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_legacy_route",
        payload={"content": "K3 怎么进入 fastboot", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_peer",
        chat_id="oc_support",
    )
    _persist_direct_answer_route(conn, event_pk)
    conn.execute(
        "UPDATE route_decisions SET knowledge_runtime_json='{}' WHERE event_pk=?",
        (event_pk,),
    )
    result = process_inbound(conn, event_pk=event_pk, worker_id="worker-1", config=cfg)
    assert result["route"]["route"] == "research"


def test_direct_answer_resume_downgrades_if_knowledge_was_revoked(conn, config):
    cfg = active_config(config)
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_direct_revoked",
        payload={"content": "K3 怎么进入 fastboot", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_peer",
        chat_id="oc_support",
    )
    knowledge_id = _persist_direct_answer_route(conn, event_pk)
    conn.execute(
        "UPDATE knowledge_entries SET status='stale' WHERE knowledge_id=?",
        (knowledge_id,),
    )

    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=cfg,
        semantic_selector=lambda *_: (_ for _ in ()).throw(
            AssertionError("persisted semantic selection must not rerun")
        ),
        message_router=lambda _: (_ for _ in ()).throw(
            AssertionError("persisted route must not rerun")
        ),
    )

    assert result["route"]["route"] == "research"


def test_conversation_meeting_creates_only_an_exact_telegram_preview(conn, config):
    cfg = active_config(config)
    start = datetime.now(UTC).replace(hour=6, minute=0, second=0, microsecond=0) + timedelta(days=1)
    end = start + timedelta(minutes=30)
    cfg.raw["features"]["calendar"] = True
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_meeting_request",
        payload={
            "content": "明天下午两点我们开半小时会讨论 UFS 启动问题",
            "chat_type": "p2p",
        },
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_requester",
        chat_id="oc_requester",
    )

    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=cfg,
        message_router=lambda _: route_value(
            "owner_decision",
            issue_type="meeting",
            reason_codes=["requires_commitment"],
            requires_owner_judgment=True,
        ),
        meeting_planner=lambda _: {
            "summary": "UFS 启动问题讨论",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "agenda": "确认现象、影响范围和下一步",
            "include_requester": True,
            "confidence": 0.97,
        },
        calendar_runner=lambda argv, **kwargs: CommandResult(
            {
                "calendars": [
                    {
                        "user_id": "ou_owner",
                        "calendar": {
                            "calendar_id": "fixture_calendar@example.invalid",
                            "type": "primary",
                            "role": "owner",
                            "is_deleted": False,
                            "is_third_party": False,
                        },
                    }
                ]
            },
            "user",
            [],
        ),
    )

    preview = result["meeting_preview"]
    assert preview is not None
    approval = conn.execute(
        "SELECT status,requested_action_json FROM approvals WHERE approval_id=?",
        (preview["approval_id"],),
    ).fetchone()
    assert approval["status"] == "requested"
    assert "ou_requester" in approval["requested_action_json"]
    assert conn.execute("SELECT count(*) FROM meeting_previews").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    payload = json.loads(conn.execute("SELECT payload_json FROM outbox").fetchone()[0])
    assert payload["approval_type"] == "meeting_create"
    assert [button["text"] for button in payload["buttons"]][:2] == [
        "查看完整预览",
        "❌ 不创建",
    ]
    assert result["job_ids"] == []
    assert (
        conn.execute(
            "SELECT route FROM route_decisions WHERE event_pk=?", (event_pk,)
        ).fetchone()[0]
        == "owner_decision"
    )


def test_conversation_meeting_does_not_guess_an_ambiguous_time(conn, config):
    cfg = active_config(config)
    cfg.raw["features"]["calendar"] = True
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_meeting_ambiguous",
        payload={"content": "找个时间聊一下 UFS", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_requester",
        chat_id="oc_requester",
    )

    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=cfg,
        message_router=lambda _: route_value(
            "owner_decision",
            issue_type="meeting",
            reason_codes=["requires_commitment"],
            requires_owner_judgment=True,
        ),
        meeting_planner=lambda _: {
            "summary": "UFS 讨论",
            "start": "",
            "end": "",
            "agenda": "确认问题",
            "include_requester": True,
            "confidence": 0.55,
        },
    )

    assert result["meeting_preview"] is None
    assert conn.execute("SELECT count(*) FROM meeting_previews").fetchone()[0] == 0
    row = conn.execute("SELECT action_type,payload_json FROM outbox").fetchone()
    assert row["action_type"] == "owner_decision"
    assert "需要你判断" in json.loads(row["payload_json"])["text"]
