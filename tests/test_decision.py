from __future__ import annotations

from datetime import UTC, datetime

import pytest

from k3_support.decision import DecisionError, apply_decision, validate_decision
from k3_support.evidence import requester_access_for_case
from k3_support.routing import record_route_decision
from k3_support.store import ConflictError, create_case, ingest_event, transition_case


def test_verified_external_requester_only_gets_public_evidence_access(conn):
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_external",
        payload={},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_external",
    )
    case_id, _ = create_case(
        conn,
        title="external request",
        case_type="faq",
        severity="P3",
        confidence=0.9,
        requester_id="ou_external",
        source_event_pk=event_pk,
    )
    record_route_decision(
        conn,
        event_pk=event_pk,
        case_id=case_id,
        route={
            "route": "owner_decision",
            "proposed_route": "direct_answer",
            "confidence": 0.98,
            "issue_type": "faq",
            "severity": "P3",
            "domain": "bootloader",
            "repository_hints": [],
            "reason_codes": ["approved_knowledge_match"],
            "clarification_question": None,
            "fallback_route": None,
            "requires_owner_judgment": True,
            "model_output_digest": "f" * 64,
        },
        profile={"relationship": "external"},
    )

    assert (
        requester_access_for_case(
            conn, case_id=case_id, visibility="internal", acl_verified=True
        )
        == "unknown"
    )
    assert (
        requester_access_for_case(conn, case_id=case_id, visibility="public")
        == "allowed"
    )


def add_allowed_evidence(conn, case_id: str) -> str:
    source_id = f"src-{case_id}"
    evidence_id = f"evd-{case_id}"
    conn.execute(
        """INSERT INTO case_sources(source_id,case_id,source_type,stable_external_id,visibility,
               requester_access,authority,metadata_json) VALUES(?,?,'test','source','internal','allowed',1,'{}')""",
        (source_id, case_id),
    )
    conn.execute(
        """INSERT INTO evidence(evidence_id,case_id,source_id,evidence_layer,freshness_at,
               visibility,claim,result,created_at)
           VALUES(?,?,?,'static',datetime('now'),'internal','claim','ok',datetime('now'))""",
        (evidence_id, case_id, source_id),
    )
    return evidence_id


def reply_decision(
    case_id: str,
    version: int,
    event_pk: str,
    evidence_id: str,
    decision_id: str = "decision-1",
) -> dict:
    return {
        "decision_id": decision_id,
        "case_id": case_id,
        "expected_case_version": version,
        "intent": "reply",
        "confidence": 0.91,
        "evidence_ids": [evidence_id],
        "reply_draft": "[AI 自动回复]请检查启动日志。",
        "proposed_actions": [
            {
                "type": "feishu_reply",
                "source_message_id": "om_1",
                "source_event_pk": event_pk,
            }
        ],
        "facts": [],
        "inferences": [],
        "unknowns": [],
    }


def test_decision_rejects_low_confidence_or_missing_prefix(conn):
    case_id, _ = create_case(
        conn, title="faq", case_type="faq", severity="P3", confidence=0.9
    )
    event_pk, _ = ingest_event(
        conn,
        source="feishu_bot_im",
        identity="bot",
        external_id="om_1",
        payload={},
        occurred_at=datetime.now(UTC).isoformat(),
    )
    value = reply_decision(case_id, 1, event_pk, add_allowed_evidence(conn, case_id))
    value["confidence"] = 0.84
    with pytest.raises(DecisionError):
        validate_decision(value)
    value["confidence"] = 0.9
    value["reply_draft"] = "没有 AI 标记"
    with pytest.raises(DecisionError):
        validate_decision(value)


def test_decision_rejects_instance_specific_internal_paths(config):
    value = reply_decision("K3-20260901-0001", 1, "evt", "evd")
    value["reply_draft"] = (
        "[AI 自动回复]内部产物位于 "
        + config.runtime("remote_worktree_root")
        + "/K3-20260901-0001"
    )
    with pytest.raises(DecisionError, match="internal absolute path"):
        validate_decision(value, config=config)


def test_apply_decision_updates_state_and_outbox_atomically(conn):
    event_pk, _ = ingest_event(
        conn,
        source="feishu_bot_im",
        identity="bot",
        external_id="om_1",
        payload={},
        occurred_at=datetime.now(UTC).isoformat(),
    )
    case_id, _ = create_case(
        conn,
        title="faq",
        case_type="faq",
        severity="P3",
        confidence=0.9,
        source_event_pk=event_pk,
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="ready",
        expected_version=1,
    )
    evidence_id = add_allowed_evidence(conn, case_id)
    result = apply_decision(conn, reply_decision(case_id, 2, event_pk, evidence_id))
    assert result["applied"] is True
    assert result["version"] == 3
    row = conn.execute(
        "SELECT state,version FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    assert tuple(row) == ("answering", 3)
    outbox = conn.execute(
        "SELECT state,payload_json FROM outbox WHERE case_id=?", (case_id,)
    ).fetchone()
    assert outbox["state"] == "pending"
    assert "### AI 自动回复" in outbox["payload_json"]
    assert (
        apply_decision(conn, reply_decision(case_id, 2, event_pk, evidence_id))[
            "reason"
        ]
        == "duplicate"
    )


def test_stale_decision_creates_no_outbox(conn):
    event_pk, _ = ingest_event(
        conn,
        source="feishu_bot_im",
        identity="bot",
        external_id="om_1",
        payload={},
        occurred_at=datetime.now(UTC).isoformat(),
    )
    case_id, _ = create_case(
        conn,
        title="faq",
        case_type="faq",
        severity="P3",
        confidence=0.9,
        source_event_pk=event_pk,
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="ready",
        expected_version=1,
    )
    evidence_id = add_allowed_evidence(conn, case_id)
    with pytest.raises(ConflictError):
        apply_decision(conn, reply_decision(case_id, 1, event_pk, evidence_id))
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_reply_rejects_empty_or_non_disclosure_safe_evidence(conn):
    event_pk, _ = ingest_event(
        conn,
        source="feishu_bot_im",
        identity="bot",
        external_id="om_1",
        payload={},
        occurred_at=datetime.now(UTC).isoformat(),
    )
    case_id, _ = create_case(
        conn,
        title="faq",
        case_type="faq",
        severity="P3",
        confidence=0.9,
        source_event_pk=event_pk,
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="ready",
        expected_version=1,
    )
    empty = reply_decision(case_id, 2, event_pk, "evd-missing")
    empty["evidence_ids"] = []
    with pytest.raises(DecisionError, match="at least one evidence"):
        validate_decision(empty)
    source_id = "src-denied"
    conn.execute(
        """INSERT INTO case_sources(source_id,case_id,source_type,stable_external_id,visibility,
               requester_access,authority,metadata_json) VALUES(?,?,'test','private','private','denied',1,'{}')""",
        (source_id, case_id),
    )
    conn.execute(
        """INSERT INTO evidence(evidence_id,case_id,source_id,evidence_layer,freshness_at,
               visibility,claim,result,created_at)
           VALUES('evd-denied',?,?,'static',datetime('now'),'private','claim','ok',datetime('now'))""",
        (case_id, source_id),
    )
    with pytest.raises(DecisionError, match="disclosure-safe"):
        apply_decision(conn, reply_decision(case_id, 2, event_pk, "evd-denied"))


def test_unimplemented_side_effect_rolls_back_entire_decision(conn):
    case_id, _ = create_case(
        conn, title="bug", case_type="bug", severity="P2", confidence=0.2
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="ready",
        expected_version=1,
    )
    decision = {
        "decision_id": "decision-bad-action",
        "case_id": case_id,
        "expected_case_version": 2,
        "intent": "delegate_codex",
        "confidence": 0.5,
        "evidence_ids": [],
        "reply_draft": None,
        "proposed_actions": [{"type": "create_codex_job"}],
        "facts": [],
        "inferences": [],
        "unknowns": [],
    }
    with pytest.raises(DecisionError):
        apply_decision(conn, decision)
    row = conn.execute(
        "SELECT state,version FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    assert tuple(row) == ("triage", 2)
