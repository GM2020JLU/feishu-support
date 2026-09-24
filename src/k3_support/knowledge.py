from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC
from typing import Any

from .db import transaction
from .ids import canonical_json, digest, new_id
from .timeutil import iso_now, parse_iso


class KnowledgeError(ValueError):
    pass


SemanticSelector = Callable[
    [str, list[dict[str, Any]]], dict[str, Any] | None
]


DISCLOSURE_LEVELS = {"public", "internal", "team", "private", "restricted"}
_DISCLOSURE_RANK = {
    "public": 0,
    "internal": 1,
    "team": 2,
    "private": 3,
    "restricted": 4,
}
_SOURCE_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@contextmanager
def _source_transaction(conn: sqlite3.Connection):
    """Allow a refresh lease and its source update to commit together."""
    if not conn.in_transaction:
        with transaction(conn):
            yield
        return
    savepoint = new_id("source")
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {savepoint}")
        raise
    finally:
        conn.execute(f"RELEASE {savepoint}")


def register_source(
    conn: sqlite3.Connection,
    *,
    source_type: str,
    stable_external_id: str,
    title: str | None,
    url: str | None,
    acl: dict[str, Any],
    source_version: str | None,
    content_digest: str | None,
    updated_at: str | None,
    checked_at: str | None = None,
) -> dict[str, Any]:
    """Register a source coordinate without treating its content as approved evidence."""
    source_type = source_type.strip().lower()
    stable_external_id = stable_external_id.strip()
    if not _SOURCE_TYPE_RE.fullmatch(source_type):
        raise KnowledgeError("invalid source_type")
    if not stable_external_id or len(stable_external_id) > 512:
        raise KnowledgeError("stable_external_id must be 1..512 characters")
    if not isinstance(acl, dict):
        raise KnowledgeError("source ACL must be a JSON object")
    visibility = str(acl.get("visibility", "private"))
    if visibility not in DISCLOSURE_LEVELS:
        raise KnowledgeError("source ACL has invalid visibility")
    normalized_acl = dict(acl)
    normalized_acl["visibility"] = visibility
    if content_digest is not None:
        content_digest = content_digest.strip().lower()
        if not _SHA256_RE.fullmatch(content_digest):
            raise KnowledgeError("content_digest must be a lowercase SHA-256")
    now = parse_iso(checked_at).astimezone(UTC).isoformat() if checked_at else iso_now()
    with _source_transaction(conn):
        existing = conn.execute(
            """SELECT source_id,source_version,content_digest,acl_json,url
                 FROM source_registry WHERE source_type=? AND stable_external_id=?""",
            (source_type, stable_external_id),
        ).fetchone()
        if existing is None:
            source_id = new_id("srg")
            conn.execute(
                """INSERT INTO source_registry(source_id,source_type,stable_external_id,title,url,
                       acl_json,source_version,content_digest,updated_at,last_checked_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id,
                    source_type,
                    stable_external_id,
                    title,
                    url,
                    canonical_json(normalized_acl),
                    source_version,
                    content_digest,
                    updated_at,
                    now,
                ),
            )
            created = True
        else:
            source_id = str(existing["source_id"])
            old_acl = json.loads(existing["acl_json"])
            source_drifted = bool(
                (
                    existing["source_version"] is not None
                    and source_version is not None
                    and existing["source_version"] != source_version
                )
                or (
                    existing["content_digest"] is not None
                    and content_digest is not None
                    and existing["content_digest"] != content_digest
                )
                or old_acl != normalized_acl
                or existing["url"] != url
            )
            conn.execute(
                """UPDATE source_registry
                      SET title=?,url=?,acl_json=?,source_version=coalesce(?,source_version),
                          content_digest=coalesce(?,content_digest),updated_at=?,last_checked_at=?
                    WHERE source_id=?""",
                (
                    title,
                    url,
                    canonical_json(normalized_acl),
                    source_version,
                    content_digest,
                    updated_at,
                    now,
                    source_id,
                ),
            )
            if source_drifted:
                conn.execute(
                    """UPDATE knowledge_entries SET status='stale',updated_at=?
                       WHERE status='approved' AND knowledge_id IN (
                           SELECT knowledge_id FROM knowledge_sources
                            WHERE source_type=? AND stable_external_id=?
                       )""",
                    (now, source_type, stable_external_id),
                )
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='professional_knowledge_revisions'"
                ).fetchone():
                    conn.execute(
                        """UPDATE professional_knowledge_revisions
                              SET lifecycle_state='needs_review'
                            WHERE lifecycle_state='published' AND revision_id IN (
                                SELECT ke.professional_revision_id FROM knowledge_entries ke
                                JOIN knowledge_sources ks ON ks.knowledge_id=ke.knowledge_id
                                WHERE ks.source_type=? AND ks.stable_external_id=?
                            )""",
                        (source_type, stable_external_id),
                    )
            created = False
    return {"source_id": source_id, "created": created}


def list_registered_sources(
    conn: sqlite3.Connection,
    *,
    source_type: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM source_registry"
    args: list[Any] = []
    if source_type:
        sql += " WHERE source_type=?"
        args.append(source_type.strip().lower())
    sql += " ORDER BY coalesce(updated_at,last_checked_at) DESC,source_id LIMIT ?"
    args.append(max(1, min(limit, 1000)))
    rows: list[dict[str, Any]] = []
    for row in conn.execute(sql, args):
        item = dict(row)
        item["acl"] = json.loads(item.pop("acl_json"))
        rows.append(item)
    return rows


def attach_registered_source(
    conn: sqlite3.Connection,
    *,
    knowledge_id: str,
    source_type: str,
    stable_external_id: str,
    claim: str,
) -> dict[str, Any]:
    """Attach a registered coordinate to a candidate without approving the answer."""
    claim = claim.strip()
    if not claim:
        raise KnowledgeError("source claim is required")
    with transaction(conn):
        knowledge = conn.execute(
            "SELECT status,disclosure_class FROM knowledge_entries WHERE knowledge_id=?",
            (knowledge_id,),
        ).fetchone()
        if knowledge is None:
            raise KnowledgeError("knowledge entry not found")
        if knowledge["status"] != "candidate":
            raise KnowledgeError("sources may only be attached to candidate knowledge")
        source = conn.execute(
            """SELECT * FROM source_registry
                 WHERE source_type=? AND stable_external_id=?""",
            (source_type.strip().lower(), stable_external_id.strip()),
        ).fetchone()
        if source is None:
            raise KnowledgeError("registered source not found")
        acl = json.loads(source["acl_json"])
        visibility = str(acl.get("visibility", "private"))
        if visibility not in DISCLOSURE_LEVELS:
            raise KnowledgeError("registered source has invalid visibility")
        if _DISCLOSURE_RANK[str(knowledge["disclosure_class"])] < _DISCLOSURE_RANK[visibility]:
            raise KnowledgeError("knowledge disclosure is broader than its source")
        existing = conn.execute(
            """SELECT mapping_id FROM knowledge_sources
                 WHERE knowledge_id=? AND source_type=? AND stable_external_id=? AND claim=?""",
            (knowledge_id, source["source_type"], source["stable_external_id"], claim),
        ).fetchone()
        if existing is not None:
            return {"mapping_id": str(existing["mapping_id"]), "created": False}
        mapping_id = new_id("ksm")
        conn.execute(
            """INSERT INTO knowledge_sources(mapping_id,knowledge_id,source_type,
                   stable_external_id,url,source_version,visibility,claim)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                mapping_id,
                knowledge_id,
                source["source_type"],
                source["stable_external_id"],
                source["url"],
                source["source_version"],
                visibility,
                claim,
            ),
        )
    return {"mapping_id": mapping_id, "created": True}


def create_candidate(
    conn: sqlite3.Connection,
    *,
    title: str,
    questions: list[str],
    answer_markdown: str,
    project: str | None,
    module: str | None,
    software_version: str | None,
    disclosure_class: str,
    confidence: float,
    source_authority: float,
    canonical_case_id: str | None,
    source_digest: str,
) -> str:
    if not questions or not answer_markdown.strip():
        raise KnowledgeError("candidate needs questions and answer")
    knowledge_id = new_id("knw")
    now = iso_now()
    content = {
        "answer": answer_markdown,
        "module": module,
        "project": project,
        "questions": questions,
        "version": software_version,
    }
    with transaction(conn):
        existing = conn.execute(
            """SELECT knowledge_id FROM knowledge_entries
               WHERE source_digest=? AND canonical_case_id IS ? ORDER BY created_at LIMIT 1""",
            (source_digest, canonical_case_id),
        ).fetchone()
        if existing is not None:
            return str(existing["knowledge_id"])
        conn.execute(
            """INSERT INTO knowledge_entries(knowledge_id,title,status,question_variants_json,
                   answer_markdown,project,module,software_version,disclosure_class,confidence,
                   source_authority,source_digest,content_digest,canonical_case_id,created_at,updated_at)
               VALUES(?,?,'candidate',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                knowledge_id,
                title,
                canonical_json(questions),
                answer_markdown,
                project,
                module,
                software_version,
                disclosure_class,
                confidence,
                source_authority,
                source_digest,
                digest(content),
                canonical_case_id,
                now,
                now,
            ),
        )
    return knowledge_id


def review(conn: sqlite3.Connection, *, knowledge_id: str, reviewer_id: str, decision: str, _before_write=None) -> None:
    if decision not in {"approved", "retired", "candidate"}:
        raise KnowledgeError("invalid review decision")
    now = iso_now()
    with transaction(conn):
        if _before_write is not None:
            _before_write()
        entry = conn.execute(
            "SELECT professional_revision_id FROM knowledge_entries WHERE knowledge_id=?",
            (knowledge_id,),
        ).fetchone()
        if entry is None:
            raise KnowledgeError("knowledge entry not found")
        if entry["professional_revision_id"]:
            if decision == "approved":
                raise KnowledgeError(
                    "professional knowledge requires a reviewed new revision and evaluated release; "
                    "legacy status approval cannot publish or revive it"
                )
            conn.execute(
                "UPDATE professional_knowledge_revisions SET lifecycle_state=? WHERE revision_id=?",
                ("retired" if decision == "retired" else "needs_review", entry["professional_revision_id"]),
            )
        changed = conn.execute(
            "UPDATE knowledge_entries SET status=?,reviewed_by=?,reviewed_at=?,updated_at=? WHERE knowledge_id=?",
            (decision, reviewer_id, now, now, knowledge_id),
        )
        if changed.rowcount != 1:
            raise KnowledgeError("knowledge entry not found")


def search(
    conn: sqlite3.Connection,
    *,
    query: str,
    requester_id: str | None,
    chat_id: str | None,
    project: str | None = None,
    module: str | None = None,
    software_version: str | None = None,
    limit: int = 10,
    verified_profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    from .knowledge_runtime import query_knowledge

    scope = {key: value for key, value in {
        "product": project, "component": module, "software_version": software_version
    }.items() if value}
    count = max(1, min(limit, 100))
    return query_knowledge(
        conn, query=query, requester_id=requester_id, chat_id=chat_id,
        observed_scope=scope, verified_profile=verified_profile,
        options={"candidate_limit": count, "prefetch_limit": max(count, 80)},
    )["retrieved_entries"]


def approved_entry(
    conn: sqlite3.Connection,
    *,
    knowledge_id: str,
    requester_id: str | None,
    chat_id: str | None,
    query: str = "",
    verified_profile: dict[str, Any] | None = None,
    observed_scope: dict[str, str] | None = None,
    context_binding: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Load one exact approved entry after rechecking requester disclosure."""
    from .knowledge_runtime import load_approved_entry

    return load_approved_entry(
        conn, knowledge_id=knowledge_id, requester_id=requester_id, chat_id=chat_id,
        query=query, verified_profile=verified_profile, observed_scope=observed_scope,
        context_binding=context_binding,
    )


def semantic_catalog(
    conn: sqlite3.Connection,
    *,
    query: str,
    requester_id: str | None,
    chat_id: str | None,
    limit: int = 100,
    verified_profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Query-driven, disclosure-filtered metadata, never a latest-N directory."""
    from .knowledge_runtime import query_knowledge

    count = max(1, min(limit, 100))
    return query_knowledge(
        conn, query=query, requester_id=requester_id, chat_id=chat_id,
        verified_profile=verified_profile,
        options={"candidate_limit": count, "prefetch_limit": max(count, 80)},
    )["candidates"]


def semantic_match(
    conn: sqlite3.Connection,
    *,
    query: str,
    requester_id: str | None,
    chat_id: str | None,
    selector: SemanticSelector,
    minimum_confidence: float = 0.85,
    observed_scope: dict[str, str] | None = None,
    verified_profile: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    hybrid=None,
) -> dict[str, Any] | None:
    """Let AI select an approved ID, then deterministically revalidate it."""
    from .knowledge_runtime import query_knowledge

    return query_knowledge(
        conn, query=query, requester_id=requester_id, chat_id=chat_id,
        selector=selector, minimum_confidence=minimum_confidence,
        observed_scope=observed_scope, verified_profile=verified_profile,
        options=options, hybrid=hybrid,
    )["selected_entry"]


def mark_stale(conn: sqlite3.Connection, *, knowledge_id: str, reason: str) -> None:
    now = iso_now()
    with transaction(conn):
        row = conn.execute("SELECT canonical_case_id FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if row is None:
            raise KnowledgeError("knowledge entry not found")
        conn.execute(
            "UPDATE knowledge_entries SET status='stale',updated_at=? WHERE knowledge_id=? AND status='approved'",
            (now, knowledge_id),
        )
        conn.execute(
            "INSERT INTO knowledge_feedback(feedback_id,knowledge_id,case_id,verdict,detail,created_at) VALUES(?,?,?,'incorrect',?,?)",
            (new_id("kfb"), knowledge_id, row[0], reason, now),
        )


def record_delivered_use(
    conn: sqlite3.Connection, *, case_id: str, outbox_id: str
) -> str | None:
    """Count a knowledge reply exactly once, only after its delivery receipt."""
    outbox = conn.execute(
        "SELECT case_id,state,remote_message_id,claim_token,payload_json FROM outbox WHERE outbox_id=?", (outbox_id,),
    ).fetchone()
    if outbox is None or outbox["case_id"] != case_id or outbox["state"] != "delivered" or not outbox["remote_message_id"]:
        return None
    payload = json.loads(outbox["payload_json"])
    if payload.get("reply_basis") == "verified_evidence":
        return None
    provenance_ids = (payload.get("knowledge_release") or {}).get("knowledge_ids")
    if outbox["claim_token"] is not None and (not isinstance(provenance_ids, list) or len(provenance_ids) != 1):
        return None
    sources = conn.execute(
        """SELECT DISTINCT stable_external_id FROM case_sources
             WHERE case_id=? AND source_type='approved_knowledge'""",
        (case_id,),
    ).fetchall()
    source_ids = {str(source["stable_external_id"]) for source in sources}
    if provenance_ids:
        if provenance_ids[0] not in source_ids:
            return None
        knowledge_id = str(provenance_ids[0])
    elif len(source_ids) == 1:
        knowledge_id = next(iter(source_ids))
    else:
        # Legacy history without an exact mapping is unknown, not the newest
        # article currently attached to a possibly long-lived Case.
        return None
    use_id = f"kus_{digest({'case_id': case_id, 'outbox_id': outbox_id})[:32]}"
    now = iso_now()
    cursor = conn.execute(
        """INSERT OR IGNORE INTO knowledge_uses(use_id,knowledge_id,case_id,outbox_id,
               state,created_at,updated_at) VALUES(?,?,?,?,'delivered',?,?)""",
        (use_id, knowledge_id, case_id, outbox_id, now, now),
    )
    if cursor.rowcount:
        conn.execute(
            "UPDATE knowledge_entries SET use_count=use_count+1,updated_at=? WHERE knowledge_id=?",
            (now, knowledge_id),
        )
    return knowledge_id


def record_feedback(
    conn: sqlite3.Connection,
    *,
    verdict: str,
    actor_id: str,
    knowledge_id: str | None = None,
    case_id: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Apply explicit feedback; negative verdicts immediately remove auto-use."""
    if verdict not in {"helpful", "incorrect", "incomplete", "sensitive"}:
        raise KnowledgeError("invalid knowledge feedback verdict")
    if not knowledge_id and not case_id:
        raise KnowledgeError("knowledge_id or case_id is required")
    if knowledge_id is not None and case_id is not None:
        delivered = conn.execute(
            "SELECT 1 FROM knowledge_uses WHERE case_id=? AND knowledge_id=? LIMIT 1",
            (case_id, knowledge_id),
        ).fetchone()
        if delivered is None:
            raise KnowledgeError("Case has no delivered use of this knowledge entry")
    if knowledge_id is None:
        row = conn.execute(
            """SELECT knowledge_id FROM knowledge_uses WHERE case_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (case_id,),
        ).fetchone()
        if row is None:
            raise KnowledgeError("Case has no delivered knowledge answer")
        knowledge_id = str(row["knowledge_id"])
    knowledge = conn.execute(
        "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()
    if knowledge is None:
        raise KnowledgeError("knowledge entry not found")
    normalized_detail = (detail or "").strip()[:2000] or None
    feedback_id = f"kfb_{digest({'knowledge_id': knowledge_id, 'case_id': case_id, 'actor_id': actor_id, 'verdict': verdict, 'detail': normalized_detail})[:32]}"
    now = iso_now()
    with transaction(conn):
        cursor = conn.execute(
            """INSERT OR IGNORE INTO knowledge_feedback(feedback_id,knowledge_id,case_id,
                   actor_id,verdict,detail,created_at) VALUES(?,?,?,?,?,?,?)""",
            (
                feedback_id, knowledge_id, case_id, actor_id, verdict,
                normalized_detail, now,
            ),
        )
        if cursor.rowcount:
            if verdict == "helpful":
                # Helpful means useful to the reader, not field resolution.
                # Keep historical success_count untouched (legacy semantics);
                # current helpful totals come from explicit feedback records.
                if case_id:
                    conn.execute(
                        """UPDATE knowledge_uses SET state='helpful',updated_at=?
                           WHERE case_id=? AND knowledge_id=?""",
                        (now, case_id, knowledge_id),
                    )
            else:
                conn.execute(
                    """UPDATE knowledge_entries SET status='stale',
                           correction_count=correction_count+1,updated_at=?
                       WHERE knowledge_id=? AND status<>'retired'""",
                    (now, knowledge_id),
                )
                conn.execute(
                    """UPDATE professional_knowledge_revisions SET lifecycle_state='needs_review'
                         WHERE lifecycle_state='published' AND revision_id=(
                             SELECT professional_revision_id FROM knowledge_entries WHERE knowledge_id=?)""",
                    (knowledge_id,),
                )
                if case_id:
                    conn.execute(
                        """UPDATE knowledge_uses SET state='corrected',updated_at=?
                           WHERE case_id=? AND knowledge_id=?""",
                        (now, case_id, knowledge_id),
                    )
    return {
        "feedback_id": feedback_id,
        "knowledge_id": knowledge_id,
        "case_id": case_id,
        "verdict": verdict,
        "created": bool(cursor.rowcount),
        "status": (
            conn.execute(
                "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
            ).fetchone()[0]
        ),
    }
