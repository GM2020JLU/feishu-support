from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .ids import canonical_json


class KnowledgeGoldCandidateError(ValueError):
    pass


_SECRET_PATTERNS = (
    (
        re.compile(r"(?i)(token|secret|password|passwd|authorization)\s*[:=]\s*\S+"),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[PHONE]"),
)


def _redact(text: str) -> str:
    value = text
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return " ".join(value.split()).strip()


def _candidate_id(query: str, source_type: str, source_id: str) -> str:
    value = canonical_json(
        {"query": query.casefold(), "source_type": source_type, "source_id": source_id}
    )
    return "candidate-" + hashlib.sha256(value.encode()).hexdigest()[:24]


def _validator() -> Draft202012Validator:
    path = resources.files("k3_support").joinpath(
        "schemas/knowledge-evaluation-candidate-v1.json"
    )
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def candidate_from_sent_feedback(material: dict[str, Any]) -> dict[str, Any] | None:
    """Adapt private revision material; the proposed route is not adjudicated truth."""
    question = material.get("question")
    if not isinstance(question, str) or not material.get("question_available"):
        return None
    query = _redact(question)
    if not 2 <= len(query) <= 10_000:
        return None
    source_id = canonical_json({key: material[key] for key in (
        "feedback_id", "material_digest", "source_event_digest", "sent_entry_fingerprint"
    )})
    candidate = {
        "schema_version": 1,
        "id": _candidate_id(query, "reviewed_sent_feedback", source_id),
        "status": "candidate", "query": query,
        "suggestion": {
            "expected_route": "research", "answerable": False,
            "allowed_knowledge_ids": [], "allowed_claim_ids": [],
            "acceptable_abstention_reasons": [],
            "tags": ["requires_human_scope_review", "sent_feedback_revision", "route_not_adjudicated"],
        },
        "provenance": {"source_type": "reviewed_sent_feedback", "source_id": source_id},
        "review": {"decision": None, "reviewed_by": None, "reviewed_at": None,
                   "notes": "待人工确定路由、可回答性和允许引用；research 仅为候选占位，不是标准结论。原回复未复制为标准答案。"},
    }
    _validator().validate(candidate)
    return candidate


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _atomic_write_jsonl(path: Path, items: list[dict[str, Any]]) -> str:
    encoded = "".join(canonical_json(item) + "\n" for item in items).encode()
    path = path.expanduser().resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".partial-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(encoded).hexdigest()


def generate_gold_candidates(
    conn: sqlite3.Connection,
    *,
    output_path: Path,
    repository_root: Path | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    """Generate private review candidates without copying answers or declaring truth."""
    if limit < 1 or limit > 10_000:
        raise KnowledgeGoldCandidateError("limit must be between 1 and 10000")
    candidates: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    legacy_mapping: dict[str, dict[str, Any]] = {}
    title_mapping: dict[str, dict[str, Any] | None] = {}
    if repository_root is not None:
        from .professional_knowledge import lint_repository

        repository = lint_repository(repository_root)
        for article in repository["articles"]:
            migration = article.metadata.get("migration")
            if not isinstance(migration, dict):
                continue
            legacy_id = migration.get("legacy_knowledge_id")
            if isinstance(legacy_id, str):
                mapped = {
                    "stable_id": article.stable_id,
                    "claims": [
                        str(claim["id"]) for claim in article.metadata["claims"]
                    ],
                    "status": str(article.metadata["status"]),
                }
                legacy_mapping[legacy_id] = mapped
                title = str(article.metadata["title"])
                title_mapping[title] = mapped if title not in title_mapping else None

    professional = _has_table(conn, "professional_knowledge_revisions") and _has_column(
        conn, "knowledge_entries", "professional_revision_id"
    )
    title_expression = (
        "ke.title" if _has_column(conn, "knowledge_entries", "title") else "NULL"
    )
    knowledge_rows = conn.execute(

            f"""SELECT ke.knowledge_id,ke.question_variants_json,{title_expression} AS title,
                      pkr.stable_id,pkr.revision_id
                 FROM knowledge_entries ke
                 LEFT JOIN professional_knowledge_revisions pkr
                   ON pkr.revision_id=ke.professional_revision_id
                WHERE ke.status='approved' ORDER BY ke.knowledge_id"""
            if professional
            else f"""SELECT ke.knowledge_id,ke.question_variants_json,
                            {title_expression} AS title,
                            NULL AS stable_id,NULL AS revision_id
                       FROM knowledge_entries ke WHERE ke.status='approved'
                      ORDER BY ke.knowledge_id"""

    )
    for row in knowledge_rows:
        questions = json.loads(row["question_variants_json"])
        mapped = legacy_mapping.get(str(row["knowledge_id"]))
        if mapped is None and row["title"] is not None:
            mapped = title_mapping.get(str(row["title"]))
        stable_id = str(
            row["stable_id"]
            or (mapped["stable_id"] if mapped else None)
            or row["knowledge_id"]
        )
        claims = (
            [
                str(item[0])
                for item in conn.execute(
                    """SELECT local_claim_id FROM professional_knowledge_claims
                         WHERE revision_id=? ORDER BY local_claim_id""",
                    (row["revision_id"],),
                )
            ]
            if row["revision_id"]
            else (list(mapped["claims"]) if mapped else [])
        )
        for raw_question in questions:
            query = _redact(str(raw_question))[:10_000]
            normalized = re.sub(r"\W+", "", query.casefold())
            if len(normalized) < 2 or normalized in seen_queries:
                continue
            seen_queries.add(normalized)
            source_id = str(row["knowledge_id"])
            candidates.append(
                {
                    "schema_version": 1,
                    "id": _candidate_id(query, "approved_knowledge", source_id),
                    "status": "candidate",
                    "query": query,
                    "suggestion": {
                        "expected_route": "direct_answer",
                        "answerable": True,
                        "allowed_knowledge_ids": [stable_id],
                        "allowed_claim_ids": claims,
                        "acceptable_abstention_reasons": [],
                        "tags": [
                            "positive",
                            *(
                                [f"professional_mapping_{mapped['status']}"]
                                if mapped
                                else []
                            ),
                            "requires_human_scope_review",
                        ],
                    },
                    "provenance": {
                        "source_type": "approved_knowledge",
                        "source_id": source_id,
                    },
                    "review": {
                        "decision": None,
                        "reviewed_by": None,
                        "reviewed_at": None,
                        "notes": "",
                    },
                }
            )
            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break

    if len(candidates) < limit and _has_table(conn, "route_decisions"):
        route_rows = conn.execute(
            """SELECT rd.route_decision_id,rd.route,rd.review_status,c.title
                 FROM route_decisions rd JOIN cases c ON c.case_id=rd.case_id
                WHERE rd.review_status IN ('accepted','shadow') AND rd.route IN (
                    'clarify','research','codex_debug','owner_decision','urgent_notify')
                ORDER BY rd.created_at,rd.route_decision_id"""
        )
        reasons = {
            "clarify": ["ambiguous_scope"],
            "research": ["no_match"],
            "codex_debug": ["needs_live_debug"],
            "owner_decision": ["high_risk"],
            "urgent_notify": ["high_risk"],
        }
        for row in route_rows:
            query = _redact(str(row["title"]))[:10_000]
            normalized = re.sub(r"\W+", "", query.casefold())
            if len(normalized) < 2 or normalized in seen_queries:
                continue
            seen_queries.add(normalized)
            source_id = str(row["route_decision_id"])
            route = str(row["route"])
            source_type = (
                "reviewed_route_decision"
                if row["review_status"] == "accepted"
                else "shadow_route_decision"
            )
            review_tag = (
                "reviewed_route"
                if row["review_status"] == "accepted"
                else "model_suggestion_unreviewed"
            )
            candidates.append(
                {
                    "schema_version": 1,
                    "id": _candidate_id(query, source_type, source_id),
                    "status": "candidate",
                    "query": query,
                    "suggestion": {
                        "expected_route": route,
                        "answerable": False,
                        "allowed_knowledge_ids": [],
                        "allowed_claim_ids": [],
                        "acceptable_abstention_reasons": reasons[route],
                        "tags": [
                            "hard_negative",
                            review_tag,
                            "requires_human_scope_review",
                        ],
                    },
                    "provenance": {
                        "source_type": source_type,
                        "source_id": source_id,
                    },
                    "review": {
                        "decision": None,
                        "reviewed_by": None,
                        "reviewed_at": None,
                        "notes": "",
                    },
                }
            )
            if len(candidates) >= limit:
                break

    validator = _validator()
    for item in candidates:
        errors = list(validator.iter_errors(item))
        if errors:
            raise KnowledgeGoldCandidateError(errors[0].message)
    output = output_path.expanduser().resolve()
    content_digest = _atomic_write_jsonl(output, candidates)
    positive = sum(item["suggestion"]["answerable"] for item in candidates)
    return {
        "output": str(output),
        "content_digest": content_digest,
        "candidate_count": len(candidates),
        "positive_count": positive,
        "hard_negative_count": len(candidates) - positive,
        "status": "human_review_required",
        "contains_answers": False,
    }
