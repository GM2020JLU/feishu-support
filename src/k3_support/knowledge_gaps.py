"""Read-only, local knowledge-gap candidates and explicitly separate outcome facts.

Heuristic intent groups are review leads, never human labels or release evidence.
The report cannot claim that absence of an AI receipt means nobody helped a user.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from .ids import digest, new_id
from .knowledge_gold import _redact
from .knowledge_runtime import query_tokens
from .timeutil import parse_iso


class KnowledgeGapError(ValueError):
    pass


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = parse_iso(value)
    if parsed.tzinfo is None:
        raise KnowledgeGapError("report dates must include timezone")
    return parsed.astimezone(UTC)


def _in_window(value, start, end) -> bool:
    try:
        observed = _time(value)
    except (TypeError, ValueError):
        return False
    return observed is not None and (start is None or start <= observed) and observed < end


def _text(value: Any) -> str:
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                return value
            return _text(parsed) if isinstance(parsed, dict) else ""
        return value
    if isinstance(value, dict):
        for key in ("text", "content"):
            if key in value:
                found = _text(value[key])
                if found:
                    return found
    return ""


def _safe(value: Any, limit=240) -> str:
    return _redact(str(value or ""))[:limit]


def _tokens(value: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"请问|麻烦|帮我|一下|您好|怎么|如何", "", normalized)
    # Preserve numbers, versions, error codes and board names. They may be the
    # distinction that makes two superficially similar questions incompatible.
    return frozenset(token for token in query_tokens(normalized) if token not in {"redacted", "email", "phone", "ip"})


def _intent_candidates(rows: list[dict], min_repeat: int) -> list[dict]:
    groups: list[dict] = []
    exact: dict[frozenset[str], int] = {}
    postings: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        tokens = _tokens(row["query"])
        if not tokens:
            continue
        chosen = exact.get(tokens)
        if chosen is None:
            # Bounded candidate comparisons keep generic words from making
            # this report quadratic. Leaders are fixed: no transitive chaining.
            rare = sorted(tokens, key=lambda token: (len(postings[token]), token))[:3]
            candidates = sorted({index for token in rare for index in postings[token][:64]})[:64]
            matches = []
            for index in candidates:
                other = groups[index]["tokens"]
                if {token for token in tokens if any(char.isdigit() for char in token)} != {
                    token for token in other if any(char.isdigit() for char in token)
                }:
                    continue
                overlap = len(tokens & other)
                similarity = 2 * overlap / (len(tokens) + len(other))
                if overlap >= 2 and similarity >= 0.65:
                    matches.append((similarity, -index, index))
            chosen = max(matches)[2] if matches else None
        if chosen is None:
            chosen = len(groups)
            groups.append({"tokens": tokens, "members": [], "examples": []})
            for token in tokens:
                postings[token].append(chosen)
        exact[tokens] = chosen
        group = groups[chosen]
        group["members"].append(row)
        if row["query"] not in group["examples"] and len(group["examples"]) < 3:
            group["examples"].append(row["query"])
    result = []
    for group in groups:
        members = group["members"]
        # Repeated messages in one Case do not become several colleagues/issues.
        cases = {row["case_id"] for row in members if row["case_id"]}
        occurrences = len(cases) + sum(not row["case_id"] for row in members)
        if occurrences < min_repeat:
            continue
        result.append({
            "candidate_id": "gap_" + digest(sorted(row["route_decision_id"] for row in members))[:24],
            "status": "unreviewed_candidate", "human_truth": False,
            "basis": "similar observed questions without an exact linked AI reply receipt",
            "question_event_count": len(members), "distinct_case_count": len(cases),
            "independent_occurrences": occurrences, "examples": group["examples"],
            "occurrence_unit": "distinct Case IDs plus unlinked question events; not distinct people",
            "case_ids_preview": sorted(cases)[:10],
            "route_counts": dict(sorted(Counter(row["route"] for row in members).items())),
            "requires_human_review": True,
        })
    return sorted(result, key=lambda item: (-item["independent_occurrences"], -item["question_event_count"], item["candidate_id"]))


def _page(items: list[dict], page: int, page_size: int) -> dict:
    offset = (page - 1) * page_size
    return {"total": len(items), "page": page, "page_size": page_size,
            "has_more": offset + page_size < len(items), "items": items[offset:offset + page_size]}


def _ratio(numerator: int, denominator: int, *, unit: str) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None, "unit": unit}


def knowledge_gap_report(
    conn: sqlite3.Connection, *, since: str | None = None, until: str | None = None,
    page: int = 1, page_size: int = 20, min_repeat: int = 2,
    now: datetime | None = None, expected_digest: str | None = None,
) -> dict[str, Any]:
    """Read one snapshot. Date bounds are aware and half-open [since, until).

    Activity uses each record's event timestamp. Catalog/source review queues
    are current inventories (not filtered by activity dates). No body, actor
    identity, feedback prose, ACL list or source URL is returned. Caller owns
    authorization; this local report must not be posted to a support chat.
    """
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100:
        raise KnowledgeGapError("invalid report pagination")
    if type(min_repeat) is not int or not 2 <= min_repeat <= 100:
        raise KnowledgeGapError("min_repeat must be in 2..100")
    observed = now or datetime.now(UTC)
    if observed.tzinfo is None:
        raise KnowledgeGapError("now must include timezone")
    observed = observed.astimezone(UTC)
    start, end = _time(since), _time(until) or observed
    if start is not None and start >= end:
        raise KnowledgeGapError("since must precede until")
    checkpoint = new_id("gap_read")
    conn.execute(f"SAVEPOINT {checkpoint}")
    try:
        routes = [dict(row) for row in conn.execute(
            """SELECT rd.route_decision_id,rd.event_pk,rd.case_id,rd.route,rd.proposed_route,
                      rd.knowledge_id,rd.review_status,rd.reviewed_at,rd.created_at,
                      c.title,c.state AS case_state,ie.source,
                      json_extract(ie.payload_json,'$.text') AS message_text,
                      json_extract(ie.payload_json,'$.content') AS message_content
                 FROM route_decisions rd JOIN inbound_events ie USING(event_pk)
                 LEFT JOIN cases c USING(case_id)
                WHERE ie.source IN ('feishu_bot_im','feishu_user_poll')
                  AND rd.issue_type NOT IN ('mail','meeting') ORDER BY rd.created_at,rd.route_decision_id"""
        )]
        in_period = [row for row in routes if _in_window(row["created_at"], start, end)]
        questions = [row for row in in_period if row["route"] != "ignore"]
        replies = [dict(row) for row in conn.execute(
            """SELECT outbox_id,case_id,source_event_pk,delivered_at FROM outbox
                WHERE channel='feishu_im' AND action_type='reply' AND state='delivered'
                  AND remote_message_id IS NOT NULL"""
        )]
        confirmed_event_ids = {row["source_event_pk"] for row in replies
                               if row["source_event_pk"] and _in_window(row["delivered_at"], None, observed)}
        gap_rows = []
        for row in questions:
            if row["event_pk"] in confirmed_event_ids or row["case_state"] in {"resolved", "cancelled"}:
                continue
            question = _text(row["message_text"]) or _text(row["message_content"]) or row["title"]
            gap_rows.append({**row, "query": _safe(question)})
        candidates = _intent_candidates(gap_rows, min_repeat)
        articles = [dict(row) for row in conn.execute(
            """SELECT k.knowledge_id,k.title,k.status,k.review_due_at,k.professional_revision_id,
                      k.correction_count,p.stable_id,p.lifecycle_state,p.review_due_at AS professional_due_at
                 FROM knowledge_entries k LEFT JOIN professional_knowledge_revisions p
                   ON p.revision_id=k.professional_revision_id ORDER BY k.knowledge_id"""
        )]
        published = {row["knowledge_id"] for row in articles
                     if row["status"] == "approved" and row["lifecycle_state"] == "published"}
        article_queue = []
        for row in articles:
            if row["status"] == "retired" or row["lifecycle_state"] == "retired":
                continue
            reasons = []
            if row["status"] in {"candidate", "stale"}:
                reasons.append(row["status"])
            if row["lifecycle_state"] and row["lifecycle_state"] != "published":
                reasons.append("professional_" + row["lifecycle_state"])
            due_text = row["professional_due_at"] or row["review_due_at"]
            try:
                due = _time(due_text)
            except (ValueError, TypeError):
                due = None
            if due is None:
                reasons.append("review_date_unknown")
            elif due <= observed:
                reasons.append("review_overdue")
            if reasons:
                article_queue.append({"knowledge_id": row["knowledge_id"], "stable_id": row["stable_id"],
                                      "title": _safe(row["title"]), "status": row["status"],
                                      "review_due_at": due_text, "reasons": reasons})
        sources = [dict(row) for row in conn.execute(
            """SELECT s.source_id,s.source_type,s.title,s.source_version,s.content_digest,s.last_checked_at,
                      rs.last_state,rs.last_error_type,rs.next_attempt_at,rs.failure_count,
                      EXISTS(SELECT 1 FROM knowledge_sources ks JOIN knowledge_entries k USING(knowledge_id)
                              LEFT JOIN professional_knowledge_revisions p ON p.revision_id=k.professional_revision_id
                              WHERE ks.source_type=s.source_type AND ks.stable_external_id=s.stable_external_id
                                AND (k.status='stale' OR p.lifecycle_state='needs_review')) AS linked_review_required
                 FROM source_registry s LEFT JOIN source_refresh_state rs USING(source_id)
                 ORDER BY s.source_id"""
        )]
        source_queue = []
        for row in sources:
            reasons = []
            if row["last_state"] == "changed":
                reasons.append("source_changed_requires_review")
            elif row["last_state"] == "failed":
                reasons.append("refresh_failed")
            if not row["last_checked_at"]:
                reasons.append("source_unchecked")
            if row["linked_review_required"]:
                reasons.append("linked_article_requires_review")
            if not row["source_version"] or not row["content_digest"]:
                reasons.append("source_evidence_incomplete")
            if not row["next_attempt_at"]:
                reasons.append("refresh_not_scheduled")
            try:
                due = _time(row["next_attempt_at"])
            except (ValueError, TypeError):
                due = None
                reasons.append("refresh_date_invalid")
            if due is not None and due <= observed:
                reasons.append("refresh_due")
            if reasons:
                source_queue.append({"source_id": row["source_id"], "source_type": row["source_type"],
                                     "title": _safe(row["title"]), "reasons": reasons,
                                     "last_checked_at": row["last_checked_at"],
                                     "failure_count": row["failure_count"] or 0})
        feedback = [dict(row) for row in conn.execute(
            "SELECT knowledge_id,case_id,verdict,created_at FROM knowledge_feedback ORDER BY feedback_id"
        ) if _in_window(row["created_at"], start, end)]
        feedback_counts = Counter(row["verdict"] for row in feedback)
        per_article: dict[str, Counter] = defaultdict(Counter)
        for row in feedback:
            per_article[row["knowledge_id"]][row["verdict"]] += 1
        article_by_id = {row["knowledge_id"]: row for row in articles}
        hotspots = []
        for key, counts in per_article.items():
            negatives = sum(counts[verdict] for verdict in ("incorrect", "incomplete", "sensitive"))
            if negatives:
                hotspots.append({"knowledge_id": key, "title": _safe(article_by_id[key]["title"]),
                                 "negative_feedback_count": negatives, "verdict_counts": dict(sorted(counts.items())),
                                 "legacy_lifetime_correction_counter": article_by_id[key]["correction_count"]})
        hotspots.sort(key=lambda row: (-row["negative_feedback_count"], row["knowledge_id"]))
        uses = [dict(row) for row in conn.execute(
            """SELECT ku.case_id,ku.outbox_id,ku.state,ku.created_at,o.state AS outbox_state,o.remote_message_id
                 FROM knowledge_uses ku JOIN outbox o USING(outbox_id) ORDER BY ku.use_id"""
        ) if _in_window(row["created_at"], start, end)]
        delivered_uses = sum(row["outbox_state"] == "delivered" and bool(row["remote_message_id"]) for row in uses)
        cohort_ids = {row["case_id"] for row in questions if row["case_id"]}
        cohort = [dict(row) for row in conn.execute("SELECT case_id,state,outcome,outcome_provenance FROM cases")
                  if row["case_id"] in cohort_ids]
        reviewed_routes = [row for row in routes if row["review_status"] in {"accepted", "rejected"}
                           and _in_window(row["reviewed_at"], start, end)]
        selected = sum(bool(row["knowledge_id"]) for row in questions)
        selected_published = sum(row["knowledge_id"] in published for row in questions)
        content = {
            "window": {"since": since, "until": until, "bounds": "inclusive start, exclusive end; until defaults to observation time"},
            "scope": {"question_events": len(questions), "nonignored_chat_routes_only": True,
                      "distinct_cases": len(cohort_ids), "ignored_route_events": len(in_period) - len(questions),
                      "knowledge_catalog_entries": len(articles), "source_registry_entries": len(sources),
                      "review_queues_are_current_inventory": True},
            "coverage": {
                "knowledge_selected": _ratio(selected, len(questions), unit="nonignored routed chat question events"),
                "currently_published_professional_selected": _ratio(selected_published, len(questions), unit="nonignored routed chat question events"),
                "exact_linked_ai_reply_received": _ratio(sum(row["event_pk"] in confirmed_event_ids for row in questions), len(questions), unit="nonignored routed chat question events"),
                "not_a_resolution_rate": True, "publication_is_not_signed_release_readiness": True,
            },
            "feedback": {"total": len(feedback), "verdict_counts": {name: feedback_counts[name] for name in ("helpful", "incorrect", "incomplete", "sensitive")},
                         "negative_feedback_count": sum(feedback_counts[name] for name in ("incorrect", "incomplete", "sensitive")),
                         "reviewed_route_count": len(reviewed_routes),
                         "rejected_route_count": sum(row["review_status"] == "rejected" for row in reviewed_routes),
                         "observed_route_override_count": sum(row["route"] != row["proposed_route"] for row in reviewed_routes),
                         "route_review_is_latest_state_not_full_edit_history": True},
            "outcomes": {
                "helpful": {"count": feedback_counts["helpful"], "unit": "explicit feedback records in window"},
                "delivered": {"count": sum(_in_window(row["delivered_at"], start, end) for row in replies), "unit": "AI reply Outbox receipts in window"},
                "knowledge_delivered_uses": {"count": delivered_uses, "unit": "knowledge uses with verified Outbox receipt, by use timestamp"},
                "local_not_reproduced": {"count": None, "evidence_status": "not_recorded", "reason": "No dedicated structured fact; board success is not proof of non-reproduction."},
                "field_resolved": {"count": None, "evidence_status": "not_recorded", "reason": "No dedicated field-confirmation fact; operator close is not a field success assertion."},
                "operator_resolved": {"count": sum(row["state"] == "resolved" and row["outcome"] == "operator_resolved" and row["outcome_provenance"] == "operator_confirmed" for row in cohort), "unit": "current operator-confirmed closure in observed Case cohort"},
                "awaiting_environment_comparison": {"count": sum(row["outcome"] == "awaiting_environment_comparison" for row in cohort), "unit": "current observed Case cohort"},
                "unknown": {"count": sum(row["outcome"] == "unknown" or row["outcome_provenance"] in {"unknown", "legacy_unknown"} for row in cohort), "unit": "current observed Case cohort"},
            },
            "aggregation": {"algorithm": "bounded_token_dice_v1", "threshold": 0.65, "max_leader_comparisons": 64,
                            "min_repeat": min_repeat, "meaning": "unreviewed similarity candidates, not semantic or human truth",
                            "missing_receipt_is_not_proof_nobody_replied": True},
            "repeated_intents": candidates, "knowledge_review_queue": article_queue,
            "source_review_queue": source_queue, "feedback_hotspots": hotspots,
        }
        fingerprint = digest(content)
        if expected_digest is not None and expected_digest != fingerprint:
            raise KnowledgeGapError("knowledge gap snapshot changed; refresh before paging")
        for section in ("repeated_intents", "knowledge_review_queue", "source_review_queue", "feedback_hotspots"):
            content[section] = _page(content[section], page, page_size)
        return {"read_only": True, "report_digest": fingerprint, "observed_at": observed.isoformat(), **content}
    finally:
        conn.execute(f"RELEASE {checkpoint}")
