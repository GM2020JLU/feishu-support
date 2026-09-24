from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config
from .db import transaction
from .ids import canonical_json, new_id
from .lark import CommandResult, LarkError, run_json
from .timeutil import epoch_now, iso_now, utc_now

ROUTES = {
    "ignore",
    "direct_answer",
    "clarify",
    "research",
    "codex_debug",
    "owner_decision",
    "urgent_notify",
}
ISSUE_TYPES = {"faq", "investigation", "bug", "incident", "request", "mail", "meeting"}
SEVERITIES = {"P0", "P1", "P2", "P3"}
RELATIONSHIPS = {
    "supervisor",
    "dotted_supervisor",
    "peer",
    "direct_report",
    "cross_function",
    "external",
    "unknown",
}
FUNCTION_ROLES = {
    "engineering",
    "project_manager",
    "product_manager",
    "qa",
    "operations",
    "management",
    "other",
    "unknown",
}
REASON_CODES = {
    "non_work_noise",
    "duplicate_or_acknowledgement",
    "context_update",
    "approved_knowledge_match",
    "missing_reproduction",
    "missing_version",
    "missing_logs",
    "missing_target",
    "source_lookup_needed",
    "technical_investigation",
    "requires_policy_decision",
    "requires_priority_decision",
    "requires_commitment",
    "requires_access_or_authority",
    "severe_outage",
    "security_or_data_risk",
    "ambiguous_request",
    "unsupported_scope",
}
CONVERSATION_RELATIONS = {
    "standalone",
    "continuation",
    "acknowledgement",
    "new_topic",
}
ROUTE_OUTPUT_FIELDS = {
    "route",
    "confidence",
    "issue_type",
    "severity",
    "domain",
    "repository_hints",
    "reason_codes",
    "clarification_question",
    "fallback_route",
    "requires_owner_judgment",
    "conversation_relation",
}
_TECHNICAL_FUNCTIONS = {"engineering", "qa", "operations"}
_HIGH_SOCIAL_RISK_RELATIONSHIPS = {"supervisor", "dotted_supervisor"}
_DECISION_FUNCTIONS = {"project_manager", "product_manager", "management"}
_COMMITMENT_MARKERS = (
    "什么时候",
    "多久",
    "排期",
    "计划",
    "进度",
    "承诺",
    "能不能支持",
    "是否支持",
    "交付",
    "eta",
    "deadline",
)


class RoutingError(ValueError):
    pass


MessageRouter = Callable[[dict[str, Any]], dict[str, Any] | None]
ClarificationReviewer = Callable[[dict[str, Any]], dict[str, Any] | None]
ContactRunner = Callable[..., CommandResult]


def unknown_profile(requester_id: str | None) -> dict[str, Any]:
    return {
        "requester_id": requester_id,
        "relationship": "unknown",
        "function_role": "unknown",
        "relationship_confidence": 0.0,
        "function_confidence": 0.0,
        "source": "unknown",
        "display_name": None,
        "department": None,
        "job_title": None,
        "verified_at": None,
    }


def _profile_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "requester_id": row["requester_id"],
        "relationship": row["relationship"],
        "function_role": row["function_role"],
        "relationship_confidence": float(row["relationship_confidence"]),
        "function_confidence": float(row["function_confidence"]),
        "source": row["source"],
        "display_name": row["display_name"],
        "department": row["department"],
        "job_title": row["job_title"],
        "verified_at": row["verified_at"],
    }


def get_requester_profile(
    conn: sqlite3.Connection, requester_id: str | None
) -> dict[str, Any]:
    if not requester_id:
        return unknown_profile(requester_id)
    row = conn.execute(
        "SELECT * FROM requester_profiles WHERE requester_id=?", (requester_id,)
    ).fetchone()
    if row is None:
        return unknown_profile(requester_id)
    return effective_requester_profile(row)


def effective_requester_profile(row, *, now=None) -> dict[str, Any]:
    """Project one cached row with the same validity rules used by routing."""
    requester_id = row["requester_id"]
    if row["source"] != "operator":
        try:
            expires = datetime.fromisoformat(str(row["expires_at"] or ""))
            valid = expires.tzinfo is not None and expires.astimezone(UTC) > (
                now or utc_now()
            )
        except ValueError:
            valid = False
        if not valid:
            # Retain the cached record for inspection, not its authority.
            profile = unknown_profile(requester_id)
            profile["display_name"] = row["display_name"]
            return profile
    return _profile_dict(row)


def set_requester_profile(
    conn: sqlite3.Connection,
    *,
    requester_id: str,
    relationship: str,
    function_role: str,
    source: str = "operator",
    relationship_confidence: float = 1.0,
    function_confidence: float = 1.0,
    display_name: str | None = None,
    department: str | None = None,
    job_title: str | None = None,
    evidence: dict[str, Any] | None = None,
    verified_at: str | None = None,
    expires_at: str | None = None,
    _before_write: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if not requester_id:
        raise RoutingError("requester_id is required")
    if relationship not in RELATIONSHIPS:
        raise RoutingError("invalid relationship")
    if function_role not in FUNCTION_ROLES:
        raise RoutingError("invalid function_role")
    if source not in {"operator", "feishu_contact", "derived", "unknown"}:
        raise RoutingError("invalid profile source")
    for value in (relationship_confidence, function_confidence):
        if isinstance(value, bool) or not 0 <= float(value) <= 1:
            raise RoutingError("profile confidence must be between 0 and 1")
    now = iso_now()
    with transaction(conn):
        if _before_write is not None:
            _before_write()
        conn.execute(
            """INSERT INTO requester_profiles(requester_id,relationship,function_role,
                   relationship_confidence,function_confidence,source,display_name,department,
                   job_title,evidence_json,verified_at,expires_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(requester_id) DO UPDATE SET
                   relationship=excluded.relationship,function_role=excluded.function_role,
                   relationship_confidence=excluded.relationship_confidence,
                   function_confidence=excluded.function_confidence,source=excluded.source,
                   display_name=excluded.display_name,department=excluded.department,
                   job_title=excluded.job_title,evidence_json=excluded.evidence_json,
                   verified_at=excluded.verified_at,expires_at=excluded.expires_at,
                   updated_at=excluded.updated_at""",
            (
                requester_id,
                relationship,
                function_role,
                float(relationship_confidence),
                float(function_confidence),
                source,
                display_name,
                department,
                job_title,
                canonical_json(evidence or {}),
                verified_at,
                expires_at,
                now,
            ),
        )
    return get_requester_profile(conn, requester_id)


def _contact_user(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RoutingError("contact result is not an object")
    value = data.get("user")
    if value is None and isinstance(data.get("data"), dict):
        value = data["data"].get("user")
    if not isinstance(value, dict) or not value.get("open_id"):
        raise RoutingError("contact result has no user")
    return value


def _fetch_contact_user(user_id: str, runner: ContactRunner) -> dict[str, Any]:
    result = runner(
        [
            "api",
            "GET",
            f"/open-apis/contact/v3/users/{user_id}",
            "--params",
            canonical_json(
                {
                    "user_id_type": "open_id",
                    "department_id_type": "open_department_id",
                }
            ),
            "--as",
            "user",
            "--format",
            "json",
        ]
    )
    if result.identity != "user":
        raise RoutingError("contact lookup did not use user identity")
    return _contact_user(result.data)


def _search_contact_user(user_id: str, runner: ContactRunner) -> dict[str, Any]:
    """Fetch the limited profile available to the signed-in Feishu user."""
    result = runner(
        [
            "contact",
            "+search-user",
            "--user-ids",
            user_id,
            "--as",
            "user",
            "--format",
            "json",
        ]
    )
    if result.identity != "user":
        raise RoutingError("contact lookup did not use user identity")
    data = result.data
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, list) or len(users) != 1 or not isinstance(users[0], dict):
        raise RoutingError("contact search returned no unique user")
    user = dict(users[0])
    if user.get("open_id") != user_id:
        raise RoutingError("contact search returned a different user")
    return user


def _function_from_contact(user: dict[str, Any]) -> tuple[str, float]:
    text = " ".join(
        str(user.get(key) or "")
        for key in ("job_title", "department", "department_path", "department_ids")
    ).lower()
    if any(
        word in text
        for word in (
            "项目经理",
            "项目管理",
            "project manager",
            "program manager",
            "pmo",
        )
    ):
        return "project_manager", 0.9
    if any(
        word in text
        for word in ("产品经理", "产品管理", "product manager", "product management")
    ):
        return "product_manager", 0.9
    if any(
        word in text
        for word in ("测试", "质量", "qa", "qe", "validation", "verification")
    ):
        return "qa", 0.85
    if any(word in text for word in ("运维", "sre", "operations", "devops")):
        return "operations", 0.85
    if any(
        word in text
        for word in ("经理", "总监", "负责人", "director", "manager", "head")
    ):
        return "management", 0.75
    if any(
        word in text
        for word in (
            "研发",
            "软件",
            "固件",
            "工程师",
            "developer",
            "engineer",
            "firmware",
        )
    ):
        return "engineering", 0.8
    return "unknown", 0.0


def _department_text(user: dict[str, Any]) -> str | None:
    paths = user.get("department_path")
    if isinstance(paths, list):
        names: list[str] = []
        for item in paths:
            if not isinstance(item, dict):
                continue
            path_name = item.get("department_path_name")
            if isinstance(path_name, dict) and path_name.get("name"):
                names.append(str(path_name["name"]))
        if names:
            return " | ".join(names)[:500]
    return None


def _relationship_from_contacts(
    *,
    requester_id: str,
    operator_id: str,
    requester: dict[str, Any],
    operator: dict[str, Any],
) -> tuple[str, float, dict[str, Any]]:
    requester_leader = requester.get("leader_user_id")
    operator_leader = operator.get("leader_user_id")
    requester_dotted = set(requester.get("dotted_line_leader_user_ids") or [])
    operator_dotted = set(operator.get("dotted_line_leader_user_ids") or [])
    evidence = {
        "requester_leader_user_id": requester_leader,
        "operator_leader_user_id": operator_leader,
        "requester_dotted_leader_match": operator_id in requester_dotted,
        "operator_dotted_leader_match": requester_id in operator_dotted,
    }
    if operator_leader == requester_id:
        return "supervisor", 1.0, evidence
    if requester_id in operator_dotted:
        return "dotted_supervisor", 1.0, evidence
    if requester_leader == operator_id or operator_id in requester_dotted:
        return "direct_report", 1.0, evidence
    if requester_leader and requester_leader == operator_leader:
        return "peer", 0.9, evidence
    requester_departments = set(requester.get("department_ids") or [])
    operator_departments = set(operator.get("department_ids") or [])
    if requester_departments & operator_departments:
        return "peer", 0.65, evidence
    if requester.get("is_cross_tenant"):
        return "external", 1.0, evidence
    return "cross_function", 0.55, evidence


def refresh_requester_profile(
    conn: sqlite3.Connection,
    config: Config,
    *,
    requester_id: str,
    runner: ContactRunner = run_json,
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT * FROM requester_profiles WHERE requester_id=?", (requester_id,)
    ).fetchone()
    if existing is not None and existing["source"] == "operator":
        return get_requester_profile(conn, requester_id)
    operator_id = config.raw["identity"].get("feishu_owner_open_id")
    if not operator_id:
        return unknown_profile(requester_id)
    operator: dict[str, Any] | None = None
    try:
        requester = _fetch_contact_user(requester_id, runner)
        operator = _fetch_contact_user(str(operator_id), runner)
    except (LarkError, RoutingError):
        try:
            requester = _search_contact_user(requester_id, runner)
        except (LarkError, RoutingError):
            return get_requester_profile(conn, requester_id)
    if operator is None:
        relationship = "external" if requester.get("is_cross_tenant") else "unknown"
        relationship_confidence = 1.0 if relationship == "external" else 0.0
        evidence = {"limited_contact_profile": True}
    else:
        relationship, relationship_confidence, evidence = _relationship_from_contacts(
            requester_id=requester_id,
            operator_id=str(operator_id),
            requester=requester,
            operator=operator,
        )
    function_role, function_confidence = _function_from_contact(requester)
    verified = iso_now()
    ttl_hours = int(config.raw["routing"]["profile_ttl_hours"])
    expires = (utc_now() + timedelta(hours=ttl_hours)).isoformat()
    class Superseded(Exception):
        pass

    def before_write():
        current = conn.execute(
            "SELECT * FROM requester_profiles WHERE requester_id=?", (requester_id,)
        ).fetchone()
        if (dict(current) if current else None) != (dict(existing) if existing else None):
            raise Superseded()

    def guarded_set(*args, **kwargs):
        try:
            return set_requester_profile(*args, **kwargs, _before_write=before_write)
        except Superseded:
            return get_requester_profile(conn, requester_id)

    return guarded_set(
        conn,
        requester_id=requester_id,
        relationship=relationship,
        function_role=function_role,
        source="feishu_contact",
        relationship_confidence=relationship_confidence,
        function_confidence=function_confidence,
        display_name=str(requester.get("name") or requester.get("localized_name") or "")
        or None,
        department=_department_text(requester)
        or str(requester.get("department") or "")
        or None,
        job_title=str(requester.get("job_title") or "") or None,
        evidence=evidence,
        verified_at=verified,
        expires_at=expires,
    )


def resolve_requester_profile(
    conn: sqlite3.Connection,
    config: Config,
    *,
    requester_id: str | None,
    runner: ContactRunner = run_json,
) -> dict[str, Any]:
    profile = get_requester_profile(conn, requester_id)
    if not requester_id or not config.raw["routing"]["org_profile_lookup"]:
        return profile
    row = conn.execute(
        "SELECT source,expires_at FROM requester_profiles WHERE requester_id=?",
        (requester_id,),
    ).fetchone()
    if row is not None and row["source"] == "operator":
        return profile
    if row is not None and row["expires_at"]:
        try:
            if datetime.fromisoformat(str(row["expires_at"])).astimezone(
                UTC
            ) > utc_now():
                return profile
        except ValueError:
            pass
    return refresh_requester_profile(
        conn, config, requester_id=requester_id, runner=runner
    )


def audience_strategy(profile: dict[str, Any]) -> dict[str, Any]:
    relationship = profile["relationship"]
    function_role = profile["function_role"]
    trusted_for_clarification = (
        profile["source"] in {"operator", "feishu_contact"}
        and float(profile["relationship_confidence"]) >= 0.85
        and float(profile["function_confidence"]) >= 0.8
    )
    if relationship in _HIGH_SOCIAL_RISK_RELATIONSHIPS:
        return {
            "tone": "concise_respectful",
            "detail": "outcome_impact_then_evidence",
            "auto_clarify": False,
            "commitments": "owner_only",
        }
    if function_role in {"project_manager", "product_manager"}:
        return {
            "tone": "concise_business_clear",
            "detail": "impact_scope_workaround_owner_no_raw_logs",
            "auto_clarify": False,
            "commitments": "owner_only",
        }
    if function_role == "qa":
        return {
            "tone": "collaborative_precise",
            "detail": "version_reproduction_expected_actual_log_marker",
            "auto_clarify": trusted_for_clarification
            and relationship in {"peer", "direct_report", "cross_function"},
            "commitments": "avoid_unverified_eta",
        }
    if function_role in {"engineering", "operations"}:
        return {
            "tone": "technical_direct",
            "detail": "commands_versions_evidence_boundaries",
            "auto_clarify": trusted_for_clarification
            and relationship in {"peer", "direct_report", "cross_function"},
            "commitments": "avoid_unverified_eta",
        }
    return {
        "tone": "neutral_respectful",
        "detail": "short_plain_language_no_assumed_context",
        "auto_clarify": False,
        "commitments": "owner_only",
    }


def validate_route_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != ROUTE_OUTPUT_FIELDS:
        raise RoutingError("route fields do not match schema")
    if not isinstance(value["route"], str) or value["route"] not in ROUTES:
        raise RoutingError("invalid route")
    if not isinstance(value["conversation_relation"], str) or value["conversation_relation"] not in CONVERSATION_RELATIONS:
        raise RoutingError("invalid conversation relation")
    confidence = value["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise RoutingError("invalid route confidence")
    if not isinstance(value["issue_type"], str) or not isinstance(value["severity"], str) or value["issue_type"] not in ISSUE_TYPES or value["severity"] not in SEVERITIES:
        raise RoutingError("invalid issue type or severity")
    if (
        not isinstance(value["domain"], str)
        or not value["domain"]
        or len(value["domain"]) > 64
    ):
        raise RoutingError("invalid route domain")
    if not isinstance(value["repository_hints"], list) or not all(
        isinstance(item, str) and item for item in value["repository_hints"]
    ):
        raise RoutingError("invalid repository hints")
    if not isinstance(value["reason_codes"], list) or not value["reason_codes"]:
        raise RoutingError("route needs reason codes")
    if not all(isinstance(reason, str) for reason in value["reason_codes"]) or not set(value["reason_codes"]) <= REASON_CODES:
        raise RoutingError("unknown route reason code")
    question = value["clarification_question"]
    if question is not None and (
        not isinstance(question, str)
        or not question.strip()
        or len(question) > 160
        or "\n" in question
    ):
        raise RoutingError("invalid clarification question")
    fallback = value["fallback_route"]
    if fallback is not None and (not isinstance(fallback, str) or fallback not in {
        "research",
        "codex_debug",
        "owner_decision",
    }):
        raise RoutingError("invalid clarification fallback")
    if not isinstance(value["requires_owner_judgment"], bool):
        raise RoutingError("requires_owner_judgment must be boolean")
    if value["route"] == "clarify" and (question is None or fallback is None):
        raise RoutingError("clarify route needs one question and fallback")
    if value["route"] != "clarify" and question is not None:
        raise RoutingError("only clarify route may carry a question")
    reasons = set(value["reason_codes"])
    if value["route"] == "direct_answer" and "approved_knowledge_match" not in reasons:
        raise RoutingError("direct answer needs an approved knowledge match")
    if value["route"] == "urgent_notify" and not reasons.intersection(
        {"severe_outage", "security_or_data_risk"}
    ):
        raise RoutingError("urgent notification needs severe evidence")
    if value["route"] == "clarify" and not reasons.intersection(
        {
            "missing_reproduction",
            "missing_version",
            "missing_logs",
            "missing_target",
            "ambiguous_request",
        }
    ):
        raise RoutingError("clarification needs one material missing fact")
    return dict(value)


def _fallback_decision(
    *,
    baseline: dict[str, Any],
    has_knowledge: bool,
    source: str,
    conversation_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if baseline["severity"] == "P0" or (
        source == "feishu_mail" and baseline["severity"] == "P1"
    ):
        route = "urgent_notify"
        reasons = ["severe_outage"]
    elif has_knowledge:
        route = "direct_answer"
        reasons = ["approved_knowledge_match"]
    elif source == "feishu_mail":
        route = "research"
        reasons = ["source_lookup_needed"]
    elif baseline["type"] in {"bug", "investigation"}:
        route = "codex_debug"
        reasons = ["technical_investigation"]
    else:
        route = "research"
        reasons = ["source_lookup_needed"]
    return {
        "route": route,
        "confidence": float(baseline["confidence"]),
        "issue_type": baseline["type"],
        "severity": baseline["severity"],
        "domain": "unknown",
        "repository_hints": [],
        "reason_codes": reasons,
        "clarification_question": None,
        "fallback_route": None,
        "requires_owner_judgment": False,
        "conversation_relation": (
            "new_topic"
            if conversation_context is not None
            and conversation_context.get("explicit_new_topic_marker")
            else (
                "continuation"
                if conversation_context is not None
                and not conversation_context.get("requires_semantic_relation")
                else "standalone"
            )
        ),
    }


def choose_route(
    *,
    query: str,
    source: str,
    chat_type: str | None,
    baseline: dict[str, Any],
    profile: dict[str, Any],
    knowledge: dict[str, Any] | None,
    router: MessageRouter | None,
    minimum_confidence: float,
    conversation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = {
        "message": query,
        "source": source,
        "chat_type": chat_type,
        "baseline": baseline,
        "requester_profile": profile,
        "audience_strategy": audience_strategy(profile),
        "approved_knowledge": (
            {
                "knowledge_id": knowledge["knowledge_id"],
                "title": knowledge["title"],
                "confidence": min(
                    float(knowledge["confidence"]),
                    float(knowledge["source_authority"]),
                    float(knowledge.get("semantic_match_confidence", 1.0)),
                ),
            }
            if knowledge is not None
            else None
        ),
        "recent_conversation": conversation_context,
    }
    proposed: dict[str, Any]
    model_valid = False
    try:
        raw = router(request) if router is not None else None
        proposed = validate_route_output(raw)
        model_valid = True
    except Exception:  # noqa: BLE001 - semantic routing must fail closed
        proposed = _fallback_decision(
            baseline=baseline,
            has_knowledge=knowledge is not None,
            source=source,
            conversation_context=conversation_context,
        )
    effective = dict(proposed)
    proposed_route = proposed["route"]
    text = query.lower()
    answer = str(knowledge.get("answer_markdown") or "") if knowledge else ""
    decision_audience = profile["relationship"] in _HIGH_SOCIAL_RISK_RELATIONSHIPS or (
        profile["function_role"] in _DECISION_FUNCTIONS
    )

    # The model may describe a relationship only to the supplied recent Case.
    # Keep acknowledgement/context semantics deterministic and never invent a
    # continuation when no recent context was supplied.
    reasons = set(proposed["reason_codes"])
    if conversation_context is None:
        effective["conversation_relation"] = "standalone"
    elif conversation_context.get("explicit_new_topic_marker"):
        effective["conversation_relation"] = "new_topic"
    elif proposed_route == "ignore" and "duplicate_or_acknowledgement" in reasons:
        effective["conversation_relation"] = "acknowledgement"
    elif proposed_route == "ignore" and "context_update" in reasons:
        effective["conversation_relation"] = "continuation"

    if baseline["severity"] == "P0":
        effective.update(
            route="urgent_notify",
            issue_type="incident" if source != "feishu_mail" else "mail",
            severity="P0",
        )
    elif source == "feishu_mail" and baseline["severity"] == "P1":
        effective.update(route="urgent_notify", issue_type="mail", severity="P1")
    elif (
        (
            profile["relationship"] == "external"
            and not (
                proposed_route == "ignore"
                or (
                    proposed_route == "direct_answer"
                    and knowledge is not None
                    and knowledge.get("disclosure_class") == "public"
                )
            )
        )
        or (
            proposed_route == "direct_answer"
            and decision_audience
            and (len(answer) > 600 or "```" in answer)
        )
        or proposed["requires_owner_judgment"]
        or (
            profile["function_role"] in _DECISION_FUNCTIONS
            and any(marker in text for marker in _COMMITMENT_MARKERS)
        )
    ):
        effective.update(
            route="owner_decision",
            clarification_question=None,
            fallback_route=None,
            requires_owner_judgment=True,
        )
    elif (model_valid and proposed["confidence"] < minimum_confidence) or (
        proposed_route == "direct_answer" and knowledge is None
    ):
        effective.update(
            route="research",
            clarification_question=None,
            fallback_route=None,
        )
    elif proposed_route == "ignore" and (
        proposed["confidence"] < 0.95
        or not reasons
        <= {"non_work_noise", "duplicate_or_acknowledgement", "context_update"}
    ):
        effective.update(route="research")
    elif (
        proposed_route == "clarify"
        and profile["relationship"] in _HIGH_SOCIAL_RISK_RELATIONSHIPS
    ):
        effective.update(
            route=proposed["fallback_route"],
            clarification_question=None,
            fallback_route=None,
        )

    if (
        conversation_context is not None
        and conversation_context.get("requires_semantic_relation")
        and not conversation_context.get("explicit_new_topic_marker")
        and (
            not model_valid
            or proposed["confidence"]
            < max(
                minimum_confidence,
                0.95 if proposed_route == "ignore" else minimum_confidence,
            )
        )
    ):
        effective["conversation_relation"] = "standalone"
        if effective["route"] != "urgent_notify":
            effective.update(
                route="owner_decision",
                clarification_question=None,
                fallback_route=None,
                requires_owner_judgment=True,
            )

    effective["proposed_route"] = proposed_route
    effective["model_output_digest"] = hashlib.sha256(
        canonical_json(proposed).encode()
    ).hexdigest()
    return effective


def clarification_allowed(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    route: dict[str, Any],
    profile: dict[str, Any],
    reviewer: ClarificationReviewer | None,
    minimum_confidence: float,
    context_binding: dict[str, Any] | None = None,
    review_record_out: dict[str, Any] | None = None,
) -> bool:
    from .clarification_context import (
        build_review_context,
        review_allows_question,
        validate_clarification_review,
    )
    from .ids import digest

    if review_record_out is not None:
        review_record_out.clear()
    if route["route"] != "clarify" or not audience_strategy(profile)["auto_clarify"]:
        return False
    already = conn.execute(
        "SELECT 1 FROM outbox WHERE case_id=? AND action_type='clarify' LIMIT 1",
        (case_id,),
    ).fetchone()
    if already is not None:
        return False
    if route["confidence"] < minimum_confidence or reviewer is None:
        return False
    try:
        request = build_review_context(
            conn,
            case_id=case_id,
            context_binding=context_binding,
            question=route["clarification_question"],
            route=route,
        )
        if (
            request["requester_profile"] != profile
            or not request["available_sources_and_checks"]
        ):
            return False
        value = reviewer(json.loads(canonical_json(request)))
        if not review_allows_question(request, value, minimum_confidence):
            return False
        record = {
            "schema_version": 2,
            "request": request,
            "review": value,
            "minimum_confidence": minimum_confidence,
        }
        record["record_digest"] = digest(record)
        if not validate_clarification_review(
            conn,
            case_id=case_id,
            question=route["clarification_question"],
            record=record,
        ):
            return False
        if review_record_out is not None:
            # A callback cannot retain and mutate the returned request/review.
            review_record_out.update(json.loads(canonical_json(record)))
        return True
    except Exception:  # noqa: BLE001 - a failed second opinion rejects the question
        return False


def record_route_decision(
    conn: sqlite3.Connection,
    *,
    event_pk: str,
    case_id: str | None,
    route: dict[str, Any],
    profile: dict[str, Any],
    conversation_case_id: str | None = None,
    knowledge: dict[str, Any] | None = None,
) -> str:
    decision_id = new_id("rte")
    with transaction(conn):
        provenance = {
            key: value
            for key, value in (knowledge or {}).items()
            if key.startswith("knowledge_") and key != "knowledge_id"
        }
        if provenance:
            from .knowledge_runtime import event_input_digest

            event = conn.execute(
                "SELECT * FROM inbound_events WHERE event_pk=?", (event_pk,)
            ).fetchone()
            if event is not None:
                provenance["knowledge_event_digest"] = event_input_digest(event)
        conn.execute(
            """INSERT OR IGNORE INTO route_decisions(route_decision_id,event_pk,case_id,route,
                   proposed_route,confidence,issue_type,severity,domain,repository_hints_json,
                   reason_codes_json,clarification_question,fallback_route,
                   requires_owner_judgment,profile_snapshot_json,model_output_digest,created_at,
                   conversation_relation,conversation_case_id,knowledge_id,
                   knowledge_source_digest,knowledge_match_confidence,knowledge_runtime_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id,
                event_pk,
                case_id,
                route["route"],
                route["proposed_route"],
                float(route["confidence"]),
                route["issue_type"],
                route["severity"],
                route["domain"],
                canonical_json(route["repository_hints"]),
                canonical_json(route["reason_codes"]),
                route.get("clarification_question"),
                route.get("fallback_route"),
                int(bool(route["requires_owner_judgment"])),
                canonical_json(profile),
                route["model_output_digest"],
                iso_now(),
                route.get("conversation_relation", "standalone"),
                conversation_case_id,
                knowledge.get("knowledge_id") if knowledge is not None else None,
                knowledge.get("source_digest") if knowledge is not None else None,
                (
                    min(
                        float(knowledge["confidence"]),
                        float(knowledge["source_authority"]),
                        float(knowledge.get("semantic_match_confidence", 1.0)),
                    )
                    if knowledge is not None
                    else None
                ),
                canonical_json(provenance),
            ),
        )
        row = conn.execute(
            "SELECT route_decision_id FROM route_decisions WHERE event_pk=?",
            (event_pk,),
        ).fetchone()
    return str(row["route_decision_id"])


def _route_row(value: sqlite3.Row) -> dict[str, Any]:
    route = dict(value)
    route["repository_hints"] = json.loads(route.pop("repository_hints_json"))
    route["reason_codes"] = json.loads(route.pop("reason_codes_json"))
    route["profile_snapshot"] = json.loads(route.pop("profile_snapshot_json"))
    route["knowledge_provenance"] = json.loads(route.pop("knowledge_runtime_json"))
    return route


def route_for_event(conn: sqlite3.Connection, event_pk: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM route_decisions WHERE event_pk=?", (event_pk,)
    ).fetchone()
    return _route_row(row) if row is not None else None


def latest_route(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM route_decisions WHERE case_id=? ORDER BY created_at DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if row is None:
        return None
    return _route_row(row)


def review_route(
    conn: sqlite3.Connection,
    *,
    route_decision_id: str,
    decision: str,
    reviewer_id: str,
    note: str | None = None,
) -> dict[str, Any]:
    if decision not in {"accepted", "rejected"}:
        raise RoutingError("route review must be accepted or rejected")
    now = iso_now()
    with transaction(conn):
        row = conn.execute(
            "SELECT case_id,review_status FROM route_decisions WHERE route_decision_id=?",
            (route_decision_id,),
        ).fetchone()
        if row is None:
            raise RoutingError("route decision not found")
        if row["review_status"] == decision:
            return {
                "route_decision_id": route_decision_id,
                "status": decision,
                "changed": False,
            }
        if row["review_status"] != "shadow":
            raise RoutingError("route decision was already reviewed differently")
        conn.execute(
            """UPDATE route_decisions SET review_status=?,reviewed_by=?,reviewed_at=?,
                   review_note=? WHERE route_decision_id=?""",
            (decision, reviewer_id, now, note, route_decision_id),
        )
        if row["case_id"]:
            sequence = conn.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?",
                (row["case_id"],),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO case_events(event_id,case_id,sequence,event_type,actor_type,
                       actor_id,detail_json,idempotency_key,created_at,created_epoch)
                   VALUES(?,?,?,'route_reviewed','operator',?,?,?,?,?)""",
                (
                    new_id("cev"),
                    row["case_id"],
                    sequence,
                    reviewer_id,
                    canonical_json(
                        {
                            "route_decision_id": route_decision_id,
                            "decision": decision,
                            "note": note,
                        }
                    ),
                    f"route-review:{route_decision_id}:{decision}",
                    now,
                    epoch_now(),
                ),
            )
    return {
        "route_decision_id": route_decision_id,
        "status": decision,
        "changed": True,
    }
