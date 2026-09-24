"""Source-bound second opinions for small questions, never an authority grant.

All reads use the current durable conversation projection. An empty lookup is
not proof that information does not exist. Local checks establish consistency;
the reviewer's semantic judgment still needs independent model evaluation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .conversation_context import context_snapshot, validate_context_binding
from .ids import canonical_json, digest
from .timeutil import parse_iso

REVIEW_FIELDS = {
    "decision",
    "confidence",
    "reason_code",
    "gap",
    "source_quote",
    "research_refs",
    "impact",
    "retrievability",
}
GAPS = {
    "missing_version": "software_version",
    "missing_target": "board",
    "missing_reproduction": "reproduction",
    "missing_logs": "error_marker",
}
OPERATIONS = {
    "software_version": "select_firmware_revision",
    "board": "select_board_procedure",
    "reproduction": "reproduce_caller_steps",
    "error_marker": "locate_failure_stage",
}
_BROAD = re.compile(
    r"完整|全部|所有|全量|整个|full\b|entire\b|all\s+(?:logs?|code|source)",
    re.IGNORECASE,
)
_QUESTION_FIELDS = {
    "software_version": re.compile(r"版本|version|commit|固件号", re.IGNORECASE),
    "board": re.compile(
        r"板型|哪.{0,2}板|board\s*(?:type|model)|hardware\s+model", re.IGNORECASE
    ),
}


class ClarificationContextError(ValueError):
    pass


def _retrieved_material(job, context):
    """Read only the exact bounded artifact, never a suggestion-supplied path.

    Directory traversal uses no-follow descriptors. Hash/input consistency is
    not an OS trust boundary against another process with the same account.
    """
    directory_fd = descriptor = None
    try:
        workdir = Path(job["workdir"])
        if not workdir.is_absolute() or ".." in workdir.parts:
            return None
        directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for part in workdir.parts[1:]:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(
            "retrieval.json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 131072:
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(131073)
        if len(raw) > 131072 or hashlib.sha256(raw).hexdigest() != job["output_digest"]:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "query",
            "full_query",
            "input_binding",
            "input_binding_digest",
            "search_count",
            "has_more",
            "documents",
            "message_search_count",
            "messages",
            "fetch_errors",
        }:
            return None
        if (
            value["schema_version"] != 1
            or any(
                value[key] != context[key]
                for key in (
                    "query",
                    "full_query",
                    "input_binding",
                    "input_binding_digest",
                )
            )
            or value["has_more"] is not False
            or value["fetch_errors"] != []
        ):
            return None
        for key, count_key, maximum, required in (
            (
                "documents",
                "search_count",
                20000,
                {"title", "summary", "url", "content", "document_id", "revision_id"},
            ),
            (
                "messages",
                "message_search_count",
                4000,
                {"message_id", "chat_id", "sender_id", "content"},
            ),
        ):
            items = value[key]
            if (
                not isinstance(items, list)
                or type(value[count_key]) is not int
                or value[count_key] != len(items)
            ):
                return None
            if any(
                not isinstance(item, dict)
                or not required.issubset(item)
                or not isinstance(item["content"], str)
                or not 0 < len(item["content"]) < maximum
                for item in items
            ):
                return None
        return value
    except (OSError, ValueError, TypeError, KeyError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)


def _context_jobs(conn, case_id, snapshot):
    """Only actual completed work for this exact input/round is review context."""
    from .retrieval import validate_retrieval_binding

    result = []
    for job in conn.execute(
        """SELECT * FROM jobs WHERE case_id=? AND lifecycle_round=?
            AND state='succeeded' AND job_type IN ('retrieve','codex') ORDER BY job_id""",
        (case_id, snapshot["lifecycle_round"]),
    ):
        context = json.loads(job["context_json"])
        if job["job_type"] == "retrieve":
            bound = (
                isinstance(context.get("input_binding"), dict)
                and context["input_binding"].get("context_binding")
                == snapshot["binding"]
                and validate_retrieval_binding(conn, job["job_id"])[0]
            )
        else:
            bound = context.get("context_binding") == snapshot["binding"]
        if not bound or not re.fullmatch(
            r"[a-f0-9]{64}", str(job["output_digest"] or "")
        ):
            continue
        if job["job_type"] == "retrieve":
            material = _retrieved_material(job, context)
            if material is None:
                continue
            for row in conn.execute(
                "SELECT suggestion_id,content_json FROM case_suggestions WHERE case_id=? AND kind='next_action' ORDER BY suggestion_id",
                (case_id,),
            ):
                value = json.loads(row["content_json"])
                if (
                    value.get("action") != "review_retrieval_result"
                    or value.get("artifact_sha256") != job["output_digest"]
                    or value.get("artifact_path")
                    != str(Path(job["workdir"]) / "retrieval.json")
                ):
                    continue
                if not isinstance(value.get("documents"), list) or not isinstance(
                    value.get("messages"), list
                ):
                    continue
                result.append(
                    {
                        "ref": "research:" + job["job_id"],
                        "type": "completed_bounded_lookup",
                        "job_id": job["job_id"],
                        "output_digest": job["output_digest"],
                        "suggestion_id": row["suggestion_id"],
                        "documents": material["documents"],
                        "messages": material["messages"],
                        "lookup_query": material["query"],
                        "coverage": "Complete returned bounded keyword excerpts and message-search results only, not whole documents, conversations or the entire library. Absence here is not absence everywhere; unknown retrievability is not proof it cannot be looked up.",
                    }
                )
        else:
            row = conn.execute(
                """SELECT h.*,r.status,r.independent_checks_json FROM case_handoffs h
                JOIN codex_reviews r USING(review_id) WHERE r.job_id=? AND h.case_id=? AND h.lifecycle_round=?
                AND r.status IN ('verified','decision_applied')""",
                (job["job_id"], case_id, snapshot["lifecycle_round"]),
            ).fetchone()
            if row:
                result.append(
                    {
                        "ref": "review:" + row["review_id"],
                        "type": "recorded_investigation_handoff",
                        "job_id": job["job_id"],
                        "output_digest": job["output_digest"],
                        "content": json.loads(row["content_json"]),
                        "checks": json.loads(row["independent_checks_json"]),
                        "coverage": "A reviewed handoff is not authorization or confirmation that the caller's site works.",
                    }
                )
    return result


def _build_review_context(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    context_binding: dict | None,
    question: str,
    route: dict,
    _own_outbox_id: str | None = None,
) -> dict[str, Any]:
    from .routing import audience_strategy, get_requester_profile

    conn.execute("SAVEPOINT clarification_context_read")
    try:
        valid, reason = validate_context_binding(conn, context_binding, require_ai=True)
        if not valid:
            raise ClarificationContextError(str(reason))
        snapshot = context_snapshot(conn, context_binding["context_id"])
        case = conn.execute(
            "SELECT case_id,version,state,lifecycle_round FROM cases WHERE case_id=?",
            (case_id,),
        ).fetchone()
        if (
            not case
            or snapshot["case_id"] != case_id
            or snapshot["lifecycle_round"] != case["lifecycle_round"]
        ):
            raise ClarificationContextError("clarification Case/context round mismatch")
        if case["state"] in {
            "resolved",
            "cancelled",
            "paused",
            "takeover",
        } or not snapshot.get("collection_complete"):
            raise ClarificationContextError(
                "clarification context is not complete and actionable"
            )
        if (
            not isinstance(question, str)
            or not 1 <= len(question.strip()) <= 240
            or _BROAD.search(question)
        ):
            raise ClarificationContextError("clarification question is too broad")
        if (
            route.get("route") != "clarify"
            or route.get("clarification_question") != question
        ):
            raise ClarificationContextError("clarification question/route mismatch")
        allowed_gaps = sorted(
            {GAPS[reason] for reason in route.get("reason_codes", []) if reason in GAPS}
        )
        if len(allowed_gaps) != 1:
            raise ClarificationContextError(
                "clarification must request exactly one small missing fact"
            )
        fields = snapshot["facts"].get("fields", {})
        gap = allowed_gaps[0]
        # A supplied version/board cannot be asked again even if a model labels
        # its question as a different missing fact. Conflicts go to research/human.
        for field, detector in _QUESTION_FIELDS.items():
            if (gap == field or detector.search(question)) and fields.get(
                field, {}
            ).get("state") in {"known", "conflict"}:
                raise ClarificationContextError(
                    "clarification asks an already supplied or conflicting field"
                )
        messages = []
        for row in conn.execute(
            """SELECT m.event_pk,m.role,m.event_digest,e.external_id,e.sender_id,e.payload_json
            FROM conversation_context_members m JOIN inbound_events e USING(event_pk)
            WHERE m.context_id=? ORDER BY m.member_sequence""",
            (snapshot["context_id"],),
        ):
            payload = json.loads(row["payload_json"])
            messages.append(
                {
                    "event_pk": row["event_pk"],
                    "message_id": row["external_id"],
                    "role": row["role"],
                    "sender_id": row["sender_id"],
                    "event_digest": row["event_digest"],
                    "content": str(payload.get("content") or ""),
                    "uninspected_attachments": bool(
                        payload.get("attachments")
                        or payload.get("file_key")
                        or payload.get("image_key")
                    ),
                }
            )
        focus = next(
            (
                item
                for item in messages
                if item["event_pk"] == snapshot["focus_event_pk"]
            ),
            None,
        )
        if (
            not focus
            or focus["role"] != "colleague"
            or any(item["uninspected_attachments"] for item in messages)
        ):
            raise ClarificationContextError(
                "clarification original input or attachment inspection is incomplete"
            )
        profile = get_requester_profile(conn, focus["sender_id"])
        if not audience_strategy(profile)["auto_clarify"]:
            raise ClarificationContextError(
                "clarification audience requires human judgment"
            )
        stored_profile = conn.execute(
            "SELECT expires_at FROM requester_profiles WHERE requester_id=?",
            (focus["sender_id"],),
        ).fetchone()
        if (
            stored_profile
            and stored_profile[0]
            and parse_iso(stored_profile[0]).astimezone(UTC) <= datetime.now(UTC)
        ):
            raise ClarificationContextError("clarification audience profile expired")
        asked = [
            dict(row)
            for row in conn.execute(
                """SELECT outbox_id,state,lifecycle_round,payload_json
            FROM outbox WHERE case_id=? AND action_type='clarify' AND (? IS NULL OR outbox_id<>?)
            ORDER BY created_at,outbox_id""",
                (case_id, _own_outbox_id, _own_outbox_id),
            )
        ]
        if asked:
            raise ClarificationContextError(
                "clarification already requested for this Case"
            )
        gap_pattern = {
            "software_version": r"版本|version|commit|固件号",
            "board": r"板型|哪.{0,2}板|board|hardware",
            "reproduction": r"复现|重现|步骤|reproduc|steps",
            "error_marker": r"报错|日志|错误|error|log",
        }[gap]
        if any(
            item["role"] == "owner"
            and re.search(gap_pattern, item["content"], re.IGNORECASE)
            and re.search(
                r"[?？]|(?:请|发|提供|补充|多少|什么|哪个|哪种|can you|please)",
                item["content"],
                re.IGNORECASE,
            )
            for item in messages
        ):
            raise ClarificationContextError(
                "operator already requested this fact; do not repeat the question"
            )
        if gap == "reproduction" and re.search(
            r"(?:复现步骤|reproduction\s+steps)\s*[:：]",
            snapshot["query"],
            re.IGNORECASE,
        ):
            raise ClarificationContextError("reproduction steps already supplied")
        if gap == "error_marker" and re.search(
            r"(?:报错|错误信息|日志|error|log)\s*[:：]",
            snapshot["query"],
            re.IGNORECASE,
        ):
            raise ClarificationContextError(
                "error information already supplied; inspect it first"
            )
        checks = _context_jobs(conn, case_id, snapshot)
        value = {
            "schema_version": 2,
            "case": dict(case),
            "context_binding": snapshot["binding"],
            "original_problem": snapshot["query"],
            "messages": messages,
            "known_information": snapshot["facts"],
            "already_asked": asked,
            "available_sources_and_checks": checks,
            "retrieval_status": "bounded_records_available"
            if checks
            else "not_checked_or_not_current",
            "question": question,
            "gap": gap,
            "route": route,
            "requester_profile": profile,
            "audience_strategy": audience_strategy(profile),
            "rules": {
                "one_question_only": True,
                "must_change_next_action": True,
                "must_not_ask_retrievable_information": True,
                "must_minimize_requester_effort": True,
                "statements_are_not_measurements": True,
                "unknown_retrievability_is_not_unretrievable": True,
            },
        }
        if len(canonical_json(value).encode()) > 65536:
            raise ClarificationContextError(
                "clarification context exceeds bounded review input; no constraints were truncated"
            )
        return value
    finally:
        conn.execute("RELEASE clarification_context_read")


def build_review_context(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    context_binding: dict | None,
    question: str,
    route: dict,
) -> dict[str, Any]:
    return _build_review_context(
        conn,
        case_id=case_id,
        context_binding=context_binding,
        question=question,
        route=route,
    )


def review_allows_question(
    request: dict, value: Any, minimum_confidence: float
) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) != REVIEW_FIELDS
        or not request["available_sources_and_checks"]
    ):
        return False
    confidence = value.get("confidence")
    if (
        value["decision"] != "send"
        or value["reason_code"] != "necessary_and_minimal"
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not minimum_confidence <= confidence <= 1
        or value["gap"] != request["gap"]
        or value["retrievability"] != "not_in_available_records"
    ):
        return False
    quote = value["source_quote"]
    if not isinstance(quote, dict) or set(quote) != {"event_pk", "quote"}:
        return False
    message = next(
        (item for item in request["messages"] if item["event_pk"] == quote["event_pk"]),
        None,
    )
    if (
        not message
        or not isinstance(quote["quote"], str)
        or not 4 <= len(quote["quote"].strip()) <= 240
        or quote["quote"] not in message["content"]
    ):
        return False
    refs = value["research_refs"]
    available = {item["ref"] for item in request["available_sources_and_checks"]}
    if (
        not isinstance(refs, list)
        or not 1 <= len(refs) <= 3
        or any(not isinstance(ref, str) or ref not in available for ref in refs)
    ):
        return False
    impact = value["impact"]
    if not isinstance(impact, dict) or set(impact) != {
        "operation",
        "if_answered",
        "if_unanswered",
        "reason",
    }:
        return False
    if impact["operation"] != OPERATIONS[request["gap"]]:
        return False
    if any(
        not isinstance(impact[key], str) or not 12 <= len(impact[key].strip()) <= 300
        for key in ("if_answered", "if_unanswered", "reason")
    ):
        return False
    return impact["if_answered"].strip() != impact["if_unanswered"].strip()


def validate_clarification_review(
    conn: sqlite3.Connection, *, case_id: str, question: str, record: dict
) -> bool:
    """Recheck in the caller's send transaction; this never sends or grants rights."""
    try:
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "schema_version",
                "request",
                "review",
                "minimum_confidence",
                "record_digest",
            }
            or record["schema_version"] != 2
        ):
            return False
        if record["record_digest"] != digest(
            {key: value for key, value in record.items() if key != "record_digest"}
        ):
            return False
        saved = record["request"]
        current = build_review_context(
            conn,
            case_id=case_id,
            context_binding=saved["context_binding"],
            question=question,
            route=saved["route"],
        )
        return current == saved and review_allows_question(
            current, record["review"], record["minimum_confidence"]
        )
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error):
        return False


def validate_clarification_delivery(
    conn: sqlite3.Connection, *, item: sqlite3.Row | dict
) -> bool:
    """Only the exact enqueue's documented state projection may differ from review.

    No current Case is substituted into the historical review. All other questions,
    including cancelled ones, retain the original one-question budget.
    """
    from .message_format import format_feishu_ai_message

    conn.execute("SAVEPOINT clarification_delivery_read")
    try:
        current_outbox = conn.execute(
            "SELECT * FROM outbox WHERE outbox_id=?", (item["outbox_id"],)
        ).fetchone()
        if current_outbox is None or any(
            item[key] != current_outbox[key]
            for key in (
                "channel",
                "action_type",
                "case_id",
                "source_event_pk",
                "payload_json",
                "destination",
                "context_id",
                "context_revision",
                "context_digest",
                "lifecycle_round",
            )
        ):
            return False
        if item["channel"] != "feishu_im" or item["action_type"] != "clarify":
            return False
        payload = json.loads(item["payload_json"])
        record = payload.get("clarification_review")
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "schema_version",
                "request",
                "review",
                "minimum_confidence",
                "record_digest",
            }
            or record["schema_version"] != 2
        ):
            return False
        if record["record_digest"] != digest(
            {key: value for key, value in record.items() if key != "record_digest"}
        ):
            return False
        saved = record["request"]
        if not review_allows_question(
            saved, record["review"], record["minimum_confidence"]
        ):
            return False
        case_id, question, binding = (
            saved["case"]["case_id"],
            saved["question"],
            saved["context_binding"],
        )
        if (
            item["case_id"] != case_id
            or item["lifecycle_round"] != saved["case"]["lifecycle_round"]
        ):
            return False
        if any(
            item[key] != binding[key]
            for key in ("context_id", "context_revision", "context_digest")
        ):
            return False
        source = conn.execute(
            "SELECT external_id,identity FROM inbound_events WHERE event_pk=?",
            (item["source_event_pk"],),
        ).fetchone()
        focus = context_snapshot(conn, binding["context_id"])["focus_event_pk"]
        if (
            not source
            or item["source_event_pk"] != focus
            or item["destination"] != source["external_id"]
            or payload.get("identity") != source["identity"]
        ):
            return False
        exact = format_feishu_ai_message(
            f"[AI 助手确认]\n\n**请补充 1 项关键信息：**\n\n{question}"
        )
        if payload.get("text") != exact or payload.get("format") != "markdown":
            return False
        events = conn.execute(
            """SELECT before_state,after_state,detail_json FROM case_events
            WHERE case_id=? AND source_event_pk=? AND event_type='clarification_requested'
            AND json_extract(detail_json,'$.outbox_id')=?""",
            (case_id, focus, item["outbox_id"]),
        ).fetchall()
        if len(events) != 1:
            return False
        event = events[0]
        detail = json.loads(event["detail_json"])
        version = saved["case"]["version"]
        if (
            saved["case"]["state"] not in {"triage", "investigating"}
            or event["before_state"] != saved["case"]["state"]
            or event["after_state"] != "investigating"
            or detail.get("question") != question
            or detail.get("review_digest") != record["record_digest"]
            or detail.get("case_version_before") != version
            or detail.get("case_version_after") != version + 1
        ):
            return False
        current = _build_review_context(
            conn,
            case_id=case_id,
            context_binding=binding,
            question=question,
            route=saved["route"],
            _own_outbox_id=item["outbox_id"],
        )
        if current["case"] != {
            **saved["case"],
            "state": "investigating",
            "version": version + 1,
        }:
            return False
        return {key: value for key, value in current.items() if key != "case"} == {
            key: value for key, value in saved.items() if key != "case"
        }
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error):
        return False
    finally:
        conn.execute("RELEASE clarification_delivery_read")
