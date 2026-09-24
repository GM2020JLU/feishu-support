from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import pytest

from k3_support.config import Config, validate_config
from k3_support.knowledge import (
    KnowledgeError,
    attach_registered_source,
    create_candidate,
    list_registered_sources,
    register_source,
    review,
    search,
)
from k3_support.knowledge_corpus import build
from k3_support.orchestrator import (
    _codex_brief,
    _codex_triage_brief,
    claim_inbound,
    process_inbound,
)
from k3_support.store import ingest_event


@pytest.fixture(autouse=True)
def _verified_fixture_colleague(conn):
    # These fixtures explicitly model a known colleague, not an arbitrary ID.
    from k3_support.routing import set_requester_profile

    set_requester_profile(
        conn, requester_id="ou_colleague", relationship="peer", function_role="engineering",
        source="operator", relationship_confidence=1.0, function_confidence=1.0,
    )


def test_knowledge_requires_review_and_enforces_chat_acl(conn):
    knowledge_id = create_candidate(
        conn,
        title="K3 Fastboot",
        questions=["K3 怎么进入 fastboot", "如何进入下载模式"],
        answer_markdown="使用板卡控制器进入 Fastboot。",
        project="k3",
        module="boot",
        software_version=None,
        disclosure_class="team",
        confidence=0.95,
        source_authority=0.9,
        canonical_case_id=None,
        source_digest="source-v1",
    )
    assert search(conn, query="K3 fastboot", requester_id="ou_a", chat_id="oc_team") == []
    conn.execute(
        "UPDATE knowledge_entries SET allowed_chat_ids_json='[\"oc_team\"]' WHERE knowledge_id=?",
        (knowledge_id,),
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner-user", decision="approved")
    assert build(conn)["built"]  # Prepare the synthetic query generation.
    assert search(conn, query="K3 fastboot", requester_id="ou_a", chat_id="oc_other") == []
    hits = search(conn, query="K3 fastboot", requester_id="ou_a", chat_id="oc_team")
    assert [hit["knowledge_id"] for hit in hits] == [knowledge_id]


def test_source_registry_is_idempotent_and_attaches_only_to_candidates(conn):
    first = register_source(
        conn,
        source_type="feishu_wiki",
        stable_external_id="wikcn_k3_boot",
        title="K3 boot guide",
        url="https://example.feishu.cn/wiki/wikcn_k3_boot",
        acl={"visibility": "internal"},
        source_version="17",
        content_digest=None,
        updated_at="2026-09-02T10:00:00+08:00",
    )
    second = register_source(
        conn,
        source_type="feishu_wiki",
        stable_external_id="wikcn_k3_boot",
        title="K3 boot guide v2",
        url="https://example.feishu.cn/wiki/wikcn_k3_boot",
        acl={"visibility": "internal"},
        source_version="18",
        content_digest="a" * 64,
        updated_at="2026-09-02T11:00:00+08:00",
    )
    assert first["created"] is True
    assert second == {"source_id": first["source_id"], "created": False}
    sources = list_registered_sources(conn)
    assert len(sources) == 1
    assert sources[0]["title"] == "K3 boot guide v2"
    assert sources[0]["acl"] == {"visibility": "internal"}

    knowledge_id = create_candidate(
        conn,
        title="K3 boot",
        questions=["K3 如何启动"],
        answer_markdown="候选答案",
        project="k3",
        module="boot",
        software_version=None,
        disclosure_class="internal",
        confidence=0.7,
        source_authority=0.8,
        canonical_case_id=None,
        source_digest="k3-boot-source-v1",
    )
    attached = attach_registered_source(
        conn,
        knowledge_id=knowledge_id,
        source_type="feishu_wiki",
        stable_external_id="wikcn_k3_boot",
        claim="Source coordinate for manual review",
    )
    duplicate = attach_registered_source(
        conn,
        knowledge_id=knowledge_id,
        source_type="feishu_wiki",
        stable_external_id="wikcn_k3_boot",
        claim="Source coordinate for manual review",
    )
    assert attached["created"] is True
    assert duplicate == {"mapping_id": attached["mapping_id"], "created": False}
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    assert build(conn)["built"]  # Prepare the synthetic query generation.
    with pytest.raises(KnowledgeError, match="only be attached"):
        attach_registered_source(
            conn,
            knowledge_id=knowledge_id,
            source_type="feishu_wiki",
            stable_external_id="wikcn_k3_boot",
            claim="Changed after review",
        )


def test_source_revision_drift_stales_approved_knowledge(conn):
    register_source(
        conn,
        source_type="git",
        stable_external_id="uboot/uboot:k3-dev",
        title="K3 U-Boot",
        url=None,
        acl={"visibility": "internal"},
        source_version="commit-a",
        content_digest="a" * 64,
        updated_at="2026-09-01T10:00:00+08:00",
    )
    knowledge_id = create_candidate(
        conn,
        title="K3 boot flow",
        questions=["K3 如何启动？"],
        answer_markdown="BROM -> SPL -> OpenSBI -> U-Boot",
        project="K3",
        module="Boot",
        software_version="commit-a",
        disclosure_class="internal",
        confidence=0.95,
        source_authority=0.95,
        canonical_case_id=None,
        source_digest="boot-flow-commit-a",
    )
    attach_registered_source(
        conn,
        knowledge_id=knowledge_id,
        source_type="git",
        stable_external_id="uboot/uboot:k3-dev",
        claim="boot flow source",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    assert build(conn)["built"]  # Prepare the synthetic query generation.

    register_source(
        conn,
        source_type="git",
        stable_external_id="uboot/uboot:k3-dev",
        title="K3 U-Boot",
        url=None,
        acl={"visibility": "internal"},
        source_version="commit-b",
        content_digest="b" * 64,
        updated_at="2026-09-02T10:00:00+08:00",
    )

    assert conn.execute(
        "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()[0] == "stale"
    assert search(
        conn,
        query="K3 启动",
        requester_id="ou_colleague",
        chat_id="oc_support",
    ) == []


def test_incomplete_source_refresh_cannot_erase_version_and_bypass_staleness(conn):
    register_source(
        conn,
        source_type="git",
        stable_external_id="uboot/uboot:main",
        title="K3 U-Boot",
        url=None,
        acl={"visibility": "internal"},
        source_version="commit-a",
        content_digest="a" * 64,
        updated_at="2026-09-01T10:00:00+08:00",
    )
    knowledge_id = create_candidate(
        conn,
        title="K3 env",
        questions=["如何保存环境变量？"],
        answer_markdown="使用 saveenv。",
        project="K3",
        module="Boot",
        software_version="commit-a",
        disclosure_class="internal",
        confidence=0.95,
        source_authority=0.95,
        canonical_case_id=None,
        source_digest="env-commit-a",
    )
    attach_registered_source(
        conn,
        knowledge_id=knowledge_id,
        source_type="git",
        stable_external_id="uboot/uboot:main",
        claim="env source",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    assert build(conn)["built"]  # Prepare the synthetic query generation.

    register_source(
        conn,
        source_type="git",
        stable_external_id="uboot/uboot:main",
        title="K3 U-Boot",
        url=None,
        acl={"visibility": "internal"},
        source_version=None,
        content_digest=None,
        updated_at="2026-09-02T10:00:00+08:00",
    )
    assert conn.execute(
        "SELECT source_version FROM source_registry WHERE stable_external_id='uboot/uboot:main'"
    ).fetchone()[0] == "commit-a"
    assert conn.execute(
        "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()[0] == "approved"

    register_source(
        conn,
        source_type="git",
        stable_external_id="uboot/uboot:main",
        title="K3 U-Boot",
        url=None,
        acl={"visibility": "internal"},
        source_version="commit-b",
        content_digest="b" * 64,
        updated_at="2026-09-03T10:00:00+08:00",
    )
    assert conn.execute(
        "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()[0] == "stale"


def test_source_url_change_stales_link_only_knowledge(conn):
    register_source(
        conn,
        source_type="feishu_doc",
        stable_external_id="docx:fan-guide",
        title="Fan guide",
        url="https://example.invalid/docx/old",
        acl={"visibility": "internal"},
        source_version="1",
        content_digest="a" * 64,
        updated_at=None,
    )
    knowledge_id = create_candidate(
        conn,
        title="Fan guide link",
        questions=["风扇怎么调？"],
        answer_markdown="https://example.invalid/docx/old",
        project="K3",
        module="EC",
        software_version=None,
        disclosure_class="internal",
        confidence=0.95,
        source_authority=0.95,
        canonical_case_id=None,
        source_digest="fan-link-v1",
    )
    attach_registered_source(
        conn,
        knowledge_id=knowledge_id,
        source_type="feishu_doc",
        stable_external_id="docx:fan-guide",
        claim="link only",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    assert build(conn)["built"]  # Prepare the synthetic query generation.

    register_source(
        conn,
        source_type="feishu_doc",
        stable_external_id="docx:fan-guide",
        title="Fan guide",
        url="https://example.invalid/docx/new",
        acl={"visibility": "internal"},
        source_version="1",
        content_digest="a" * 64,
        updated_at=None,
    )

    assert conn.execute(
        "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()[0] == "stale"


def test_source_acl_tightening_stales_approved_knowledge(conn):
    register_source(
        conn,
        source_type="feishu_doc",
        stable_external_id="docx:shared-guide",
        title="Shared guide",
        url="https://example.invalid/docx/shared-guide",
        acl={"visibility": "internal"},
        source_version="1",
        content_digest="a" * 64,
        updated_at=None,
    )
    knowledge_id = create_candidate(
        conn,
        title="Shared guide route",
        questions=["guide 在哪？"],
        answer_markdown="https://example.invalid/docx/shared-guide",
        project="K3",
        module=None,
        software_version=None,
        disclosure_class="internal",
        confidence=0.95,
        source_authority=0.95,
        canonical_case_id=None,
        source_digest="shared-guide-v1",
    )
    attach_registered_source(
        conn,
        knowledge_id=knowledge_id,
        source_type="feishu_doc",
        stable_external_id="docx:shared-guide",
        claim="link only",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    assert build(conn)["built"]  # Prepare the synthetic query generation.

    register_source(
        conn,
        source_type="feishu_doc",
        stable_external_id="docx:shared-guide",
        title="Shared guide",
        url="https://example.invalid/docx/shared-guide",
        acl={"visibility": "restricted"},
        source_version="1",
        content_digest="a" * 64,
        updated_at=None,
    )

    assert conn.execute(
        "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()[0] == "stale"


def test_source_acl_cannot_be_broader_than_candidate(conn):
    register_source(
        conn,
        source_type="feishu_docx",
        stable_external_id="private_doc",
        title="Private debug notes",
        url=None,
        acl={"visibility": "private"},
        source_version=None,
        content_digest=None,
        updated_at=None,
    )
    knowledge_id = create_candidate(
        conn,
        title="Unsafe candidate",
        questions=["debug"],
        answer_markdown="candidate",
        project="k3",
        module=None,
        software_version=None,
        disclosure_class="internal",
        confidence=0.5,
        source_authority=0.5,
        canonical_case_id=None,
        source_digest="private-source-v1",
    )
    with pytest.raises(KnowledgeError, match="broader than its source"):
        attach_registered_source(
            conn,
            knowledge_id=knowledge_id,
            source_type="feishu_docx",
            stable_external_id="private_doc",
            claim="Must remain private",
        )


def test_codex_briefs_use_instance_runtime_not_developer_paths(config):
    raw = copy.deepcopy(config.raw)
    raw["runtime"].update(
        {
            "remote_host": "builder@k3-host",
            "codex_remote_command": "/opt/k3/bin/k3-codex-remote",
        }
    )
    cfg = Config(validate_config(raw), config.path)
    direct = _codex_brief(
        config=cfg,
        case_id="K3-20260901-0001",
        repo="u-boot",
        repo_path="/srv/firmware/source/u-boot",
        query="boot failure",
    )
    triage = _codex_triage_brief(
        config=cfg,
        case_id="K3-20260901-0001",
        repositories={
            "u-boot": {
                "path": "/srv/firmware/source/u-boot",
                "remote": "origin",
                "base_branch": "main",
            }
        },
        query="boot failure",
    )
    for brief in (direct, triage):
        assert "builder@k3-host" in brief
        assert "/opt/k3/bin/k3-codex-remote" in brief
        assert "/home/operator" not in brief
        assert "buildhost" not in brief


def test_shadow_orchestrator_creates_one_case_and_suggestions_without_outbox(conn):
    knowledge_id = create_candidate(
        conn,
        title="K3 Fastboot",
        questions=["K3 怎么进入 fastboot"],
        answer_markdown="先进入下载模式。",
        project="k3",
        module="boot",
        software_version=None,
        disclosure_class="internal",
        confidence=0.92,
        source_authority=0.9,
        canonical_case_id=None,
        source_digest="source-v1",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner-user", decision="approved")
    assert build(conn)["built"]  # Prepare the synthetic query generation.
    event_pk, _ = ingest_event(
        conn,
        source="feishu_bot_im",
        identity="bot",
        external_id="om_shadow_1",
        payload={"content": "K3 怎么进入 fastboot？", "message_type": "text"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_team",
    )
    first = process_inbound(conn, event_pk=event_pk, worker_id="worker-1",
                            semantic_selector=lambda query, catalog: {"knowledge_id": knowledge_id, "confidence": .99})
    second = process_inbound(conn, event_pk=event_pk, worker_id="worker-1")
    assert first["processed"] is True
    assert second == {"processed": False, "reason": "duplicate", "case_id": first["case_id"]}
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 1
    suggestions = conn.execute("SELECT content_json FROM case_suggestions WHERE case_id=?", (first["case_id"],)).fetchall()
    assert len(suggestions) == 3
    assert sum(json.loads(row[0]).get("event") == "context_route_reassessment" for row in suggestions) == 1
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_inbound_processing_respects_another_workers_active_lease(conn):
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_leased",
        payload={"content": "U-Boot 启动失败", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_p2p",
    )
    handles = claim_inbound(conn, worker_id="worker-a")
    assert handles == [event_pk]

    rejected = process_inbound(conn, event_pk=event_pk, worker_id="worker-b")
    assert rejected["processed"] is False
    assert rejected["reason"] == "claimed_by_other_worker"
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM route_decisions").fetchone()[0] == 0

    accepted = process_inbound(conn, event_pk=handles[0], worker_id="worker-a")
    assert accepted["processed"] is True
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 1


def test_p2p_followup_is_attached_to_recent_case_without_second_reply(conn):
    first_event, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_fragment_1",
        payload={
            "content": "我看了一下 U-Boot 加载内核，不压缩还能快 200ms",
            "message_type": "text",
            "chat_type": "p2p",
        },
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_p2p",
    )
    second_event, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_fragment_2",
        payload={
            "content": "我手动改了一下",
            "message_type": "text",
            "chat_type": "p2p",
        },
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_p2p",
    )

    first = process_inbound(conn, event_pk=first_event, worker_id="worker-1")
    second = process_inbound(conn, event_pk=second_event, worker_id="worker-1")

    assert second["case_id"] == first["case_id"]
    assert second["created"] is False
    assert second["merged_followup"] is True
    assert second["outbox_ids"] == []
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 1
    linked = conn.execute(
        "SELECT event_type,source_event_pk FROM case_events WHERE event_type='followup_attached'"
    ).fetchone()
    assert tuple(linked) == ("followup_attached", second_event)


def test_p2p_followup_merges_when_ingress_transport_changes(conn):
    event_ids = []
    for source, identity, external_id, content in (
        ("feishu_bot_im", "bot", "om_transport_1", "U-Boot 启动失败"),
        ("feishu_user_poll", "user", "om_transport_2", "补充一下，是 UFS 启动"),
    ):
        event_pk, _ = ingest_event(
            conn,
            source=source,
            identity=identity,
            external_id=external_id,
            payload={"content": content, "message_type": "text", "chat_type": "p2p"},
            occurred_at=datetime.now(UTC).isoformat(),
            sender_id="ou_colleague",
            chat_id="oc_p2p",
        )
        event_ids.append(event_pk)

    first = process_inbound(conn, event_pk=event_ids[0], worker_id="worker-1")
    second = process_inbound(conn, event_pk=event_ids[1], worker_id="worker-1")

    assert second["case_id"] == first["case_id"]
    assert second["merged_followup"] is True
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 1


def test_explicit_new_p2p_topic_creates_a_separate_case(conn):
    event_ids = []
    for external_id, content in (
        ("om_topic_1", "K3 启动耗时有点长"),
        ("om_topic_2", "另外一个新问题：串口偶尔没有输出"),
    ):
        event_pk, _ = ingest_event(
            conn,
            source="feishu_user_poll",
            identity="user",
            external_id=external_id,
            payload={
                "content": content,
                "message_type": "text",
                "chat_type": "p2p",
            },
            occurred_at=datetime.now(UTC).isoformat(),
            sender_id="ou_colleague",
            chat_id="oc_p2p",
        )
        event_ids.append(event_pk)

    first = process_inbound(conn, event_pk=event_ids[0], worker_id="worker-1")
    second = process_inbound(conn, event_pk=event_ids[1], worker_id="worker-1")

    assert second["case_id"] != first["case_id"]
    assert second["created"] is True
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 2


def test_group_messages_are_never_coalesced_by_p2p_heuristic(conn):
    case_ids = []
    for sequence in (1, 2):
        event_pk, _ = ingest_event(
            conn,
            source="feishu_user_poll",
            identity="user",
            external_id=f"om_group_{sequence}",
            payload={
                "content": f"群消息补充 {sequence}",
                "message_type": "text",
                "chat_type": "group",
            },
            occurred_at=datetime.now(UTC).isoformat(),
            sender_id="ou_colleague",
            chat_id="oc_group",
        )
        case_ids.append(
            process_inbound(conn, event_pk=event_pk, worker_id="worker-1")["case_id"]
        )
    assert len(set(case_ids)) == 2


def _active_config(config, *, auto_reply_chat_ids, codex=False):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"]["auto_faq"] = True
    raw["features"]["codex"] = codex
    raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    raw["scope"]["auto_reply_chat_ids"] = auto_reply_chat_ids
    return Config(validate_config(raw), config.path)


def test_active_orchestrator_auto_replies_only_from_approved_scoped_knowledge(conn, config):
    knowledge_id = create_candidate(
        conn,
        title="K3 Fastboot",
        questions=["K3 怎么进入 fastboot"],
        answer_markdown="先进入下载模式。",
        project="k3",
        module="boot",
        software_version=None,
        disclosure_class="internal",
        confidence=0.96,
        source_authority=0.95,
        canonical_case_id=None,
        source_digest="source-v2",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner-user", decision="approved")
    assert build(conn)["built"]  # Prepare the synthetic query generation.
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_active_1",
        payload={"content": "K3 怎么进入 fastboot？", "message_type": "text"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_allowed",
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=_active_config(config, auto_reply_chat_ids=["oc_allowed"]),
        semantic_selector=lambda query, catalog: {"knowledge_id": knowledge_id, "confidence": .99},
    )
    assert len(result["outbox_ids"]) == 1
    row = conn.execute("SELECT action_type,payload_json FROM outbox").fetchone()
    assert row["action_type"] == "reply"
    assert "### AI 自动回复" in row["payload_json"]
    evidence = conn.execute(
        """SELECT e.evidence_id,cs.source_type,cs.requester_access
           FROM evidence e JOIN case_sources cs USING(source_id) WHERE e.case_id=?""",
        (result["case_id"],),
    ).fetchone()
    assert evidence is not None
    assert tuple(evidence)[1:] == ("approved_knowledge", "allowed")
    assert conn.execute("SELECT state FROM cases").fetchone()[0] == "answering"


def test_active_orchestrator_allows_high_confidence_p2p_without_chat_allowlist(
    conn, config
):
    knowledge_id = create_candidate(
        conn,
        title="K3 Fastboot",
        questions=["K3 怎么进入 fastboot"],
        answer_markdown="先进入下载模式。",
        project="k3",
        module="boot",
        software_version=None,
        disclosure_class="internal",
        confidence=0.96,
        source_authority=0.95,
        canonical_case_id=None,
        source_digest="source-p2p",
    )
    review(
        conn,
        knowledge_id=knowledge_id,
        reviewer_id="owner-user",
        decision="approved",
    )
    assert build(conn)["built"]  # Prepare the synthetic query generation.
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_p2p_1",
        payload={
            "content": "K3 怎么进入 fastboot？",
            "message_type": "text",
            "chat_type": "p2p",
        },
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_dynamic_colleague_chat",
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=_active_config(config, auto_reply_chat_ids=[]),
        semantic_selector=lambda query, catalog: {"knowledge_id": knowledge_id, "confidence": .99},
    )
    assert len(result["outbox_ids"]) == 1
    row = conn.execute("SELECT action_type,payload_json FROM outbox").fetchone()
    assert row["action_type"] == "reply"
    assert "### AI 自动回复" in row["payload_json"]


def test_active_orchestrator_uses_ai_semantic_fallback_for_paraphrase(conn, config):
    knowledge_id = create_candidate(
        conn,
        title="文档路由：K3 Pico-ITX 风扇配置指南",
        questions=["Pico-ITX 的风扇怎么调？", "K3 Pico 风扇转速怎么配置？"],
        answer_markdown=(
            "参考文档：[K3 Pico-ITX 风扇配置指南]"
            "(https://example.feishu.cn/wiki/fan-guide)"
        ),
        project="K3",
        module="Fan",
        software_version=None,
        disclosure_class="internal",
        confidence=0.95,
        source_authority=0.95,
        canonical_case_id=None,
        source_digest="fan-guide-v1",
    )
    review(
        conn,
        knowledge_id=knowledge_id,
        reviewer_id="owner-user",
        decision="approved",
    )
    assert build(conn)["built"]  # Prepare the synthetic query generation.
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_semantic_fan_1",
        payload={
            "content": "小风扇一直满转太吵了，能不能让它安静一点？",
            "message_type": "text",
            "chat_type": "p2p",
        },
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_p2p",
    )

    def selector(query, catalog):
        assert "安静一点" in query
        assert [item["knowledge_id"] for item in catalog] == [knowledge_id]
        return {"knowledge_id": knowledge_id, "confidence": 0.93}

    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=_active_config(config, auto_reply_chat_ids=[]),
        semantic_selector=selector,
    )
    assert len(result["outbox_ids"]) == 1
    payload = conn.execute("SELECT payload_json FROM outbox").fetchone()[0]
    assert "K3 Pico-ITX 风扇配置指南" in payload


def test_semantic_selector_failure_does_not_fall_back_to_keyword_reply(conn, config):
    knowledge_id = create_candidate(
        conn,
        title="风扇配置",
        questions=["风扇怎么配置？"],
        answer_markdown="参考文档：https://example.invalid/fan",
        project="K3",
        module="Fan",
        software_version=None,
        disclosure_class="internal",
        confidence=0.95,
        source_authority=0.95,
        canonical_case_id=None,
        source_digest="fan-no-lexical-fallback-v1",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    assert build(conn)["built"]  # Prepare the synthetic query generation.
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_semantic_fail_closed_1",
        payload={
            "content": "风扇怎么配置？",
            "message_type": "text",
            "chat_type": "p2p",
        },
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_p2p",
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=_active_config(config, auto_reply_chat_ids=[]),
        semantic_selector=lambda query, catalog: None,
    )
    assert len(result["outbox_ids"]) == 1
    row = conn.execute("SELECT action_type FROM outbox").fetchone()
    assert row["action_type"] == "ack"


def test_ai_semantic_fallback_rejects_invented_route_id(conn, config):
    knowledge_id = create_candidate(
        conn,
        title="K3 boot flow",
        questions=["K3 启动流程是什么？"],
        answer_markdown="BROM -> SPL -> OpenSBI -> U-Boot",
        project="K3",
        module="Boot",
        software_version=None,
        disclosure_class="internal",
        confidence=0.95,
        source_authority=0.95,
        canonical_case_id=None,
        source_digest="boot-flow-v1",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    assert build(conn)["built"]  # Prepare the synthetic query generation.
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_semantic_invalid_1",
        payload={
            "content": "从上电到 Linux 中间都经过什么？",
            "message_type": "text",
            "chat_type": "p2p",
        },
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_p2p",
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=_active_config(config, auto_reply_chat_ids=[]),
        semantic_selector=lambda query, catalog: {
            "knowledge_id": "knw_invented",
            "confidence": 0.99,
        },
    )
    assert len(result["outbox_ids"]) == 1
    row = conn.execute("SELECT action_type FROM outbox").fetchone()
    assert row["action_type"] == "ack"


def test_active_orchestrator_acknowledges_when_chat_is_not_auto_reply_scoped(conn, config):
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_active_2",
        payload={"content": "K3 启动时报错，请排查", "message_type": "text"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_not_allowed",
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="worker-1",
        config=_active_config(config, auto_reply_chat_ids=[]),
    )
    assert len(result["outbox_ids"]) == 1
    row = conn.execute("SELECT action_type,payload_json FROM outbox").fetchone()
    assert row["action_type"] == "ack"
    assert result["case_id"] in row["payload_json"]
    assert conn.execute("SELECT state FROM cases").fetchone()[0] == "investigating"


def test_active_orchestrator_retrieves_before_explicit_repo_codex_job(conn, config):
    cfg = _active_config(config, auto_reply_chat_ids=[], codex=True)
    cfg.raw["repositories"] = {
        "u-boot": {
            "path": "/data/home2/operator/WorkSpace/k3/uboot-2022.10",
            "remote": "origin",
            "base_branch": "main",
        },
        "linux": {
            "path": "/data/home2/operator/WorkSpace/k3/linux-6.18",
            "remote": "origin",
            "base_branch": "main",
        },
    }
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_bug_1",
        payload={"content": "U-Boot 启动时报错，请排查", "message_type": "text"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_support",
    )
    result = process_inbound(conn, event_pk=event_pk, worker_id="worker-1", config=cfg)
    assert len(result["job_ids"]) == 1
    job = conn.execute("SELECT job_type,state FROM jobs").fetchone()
    assert tuple(job) == ("retrieve", "queued")


def test_active_orchestrator_retrieves_before_ambiguous_codex_triage(conn, config):
    cfg = _active_config(config, auto_reply_chat_ids=[], codex=True)
    cfg.raw["repositories"] = {
        "u-boot": {
            "path": "/data/home2/operator/WorkSpace/k3/uboot-2022.10",
            "remote": "origin",
            "base_branch": "main",
        }
    }
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_bug_2",
        payload={"content": "K3 启动异常，请帮忙排查", "message_type": "text"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_support",
    )
    result = process_inbound(conn, event_pk=event_pk, worker_id="worker-1", config=cfg)
    assert len(result["job_ids"]) == 1
    job = conn.execute("SELECT job_type,state FROM jobs").fetchone()
    assert tuple(job) == ("retrieve", "queued")


def test_p1_mail_alerts_telegram_but_never_uses_mail_id_as_im_reply(conn, config):
    cfg = _active_config(config, auto_reply_chat_ids=[])
    event_pk, _ = ingest_event(
        conn,
        source="feishu_mail",
        identity="user",
        external_id="mail_urgent_1:new",
        payload={"subject": "紧急：服务宕机", "body_preview": "outage", "message_type": "mail"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="alerts@example.invalid",
    )
    result = process_inbound(conn, event_pk=event_pk, worker_id="worker-1", config=cfg)
    assert len(result["outbox_ids"]) == 1
    row = conn.execute("SELECT channel,action_type,destination FROM outbox").fetchone()
    assert tuple(row) == ("telegram", "incident_alert", "telegram:owner-chat")


def test_p0_report_queues_telegram_and_dependent_feishu_urgent_route(conn, config):
    cfg = _active_config(config, auto_reply_chat_ids=[])
    cfg.raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    cfg.raw["identity"]["feishu_p0_chat_id"] = "oc_incident"
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_p0_1",
        payload={
            "content": "K3 生产环境大面积宕机并发生数据丢失",
            "message_type": "text",
            "chat_type": "p2p",
        },
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_p2p",
    )
    result = process_inbound(
        conn, event_pk=event_pk, worker_id="worker-1", config=cfg
    )
    assert result["classification"]["severity"] == "P0"
    routes = conn.execute(
        "SELECT channel,action_type,destination FROM outbox ORDER BY created_at,outbox_id"
    ).fetchall()
    assert {tuple(row) for row in routes} >= {
        ("telegram", "p0_alert", "telegram:owner-chat"),
        ("feishu_im", "send", "oc_incident"),
    }
