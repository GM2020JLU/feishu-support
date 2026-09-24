from __future__ import annotations

import json
import sqlite3
from typing import Any

from .ids import canonical_json, digest
from .timeutil import iso_now


class EvidenceError(ValueError):
    pass


def requester_access_for_case(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    visibility: str,
    acl_verified: bool = False,
) -> str:
    """Determine whether evidence may support an automatic requester reply.

    A verified external relationship fails closed for every non-public source.
    For internal requesters, narrower knowledge is allowed only after its entry
    ACL has already been checked by the knowledge lookup.
    """
    case = conn.execute(
        "SELECT requester_id FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    if case is None or not case["requester_id"]:
        return "unknown"
    route = conn.execute(
        """SELECT profile_snapshot_json FROM route_decisions
             WHERE case_id=? ORDER BY created_at DESC LIMIT 1""",
        (case_id,),
    ).fetchone()
    relationship = None
    if route is not None:
        try:
            relationship = json.loads(route[0]).get("relationship")
        except (TypeError, ValueError, AttributeError):
            relationship = None
    if relationship == "external" and visibility != "public":
        return "unknown"
    if acl_verified or visibility in {"public", "internal"}:
        return "allowed"
    return "unknown"


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{digest(value)[:32]}"


def record_knowledge_evidence(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    knowledge: dict[str, Any],
) -> str:
    """Bind one ACL-filtered approved knowledge hit to a Case as reply evidence.

    The caller obtains ``knowledge`` through ``knowledge.search`` and calls this
    inside the same transaction that prepares the Decision. Re-reading the
    durable row prevents a stale suggestion from inventing an allowed source.
    """
    knowledge_id = str(knowledge.get("knowledge_id") or "")
    row = conn.execute(
        """SELECT knowledge_id,title,status,answer_markdown,disclosure_class,
                  confidence,source_authority,source_digest,content_digest,
                  evidence_layers_json,updated_at
           FROM knowledge_entries WHERE knowledge_id=?""",
        (knowledge_id,),
    ).fetchone()
    if row is None or row["status"] != "approved":
        raise EvidenceError("knowledge evidence is not currently approved")
    if row["source_digest"] != knowledge.get("source_digest"):
        raise EvidenceError("knowledge evidence changed after retrieval")

    source_id = _stable_id(
        "src",
        {
            "case_id": case_id,
            "knowledge_id": knowledge_id,
            "source_digest": row["source_digest"],
        },
    )
    evidence_id = _stable_id(
        "evd",
        {
            "case_id": case_id,
            "source_id": source_id,
            "content_digest": row["content_digest"],
        },
    )
    now = iso_now()
    requester_access = requester_access_for_case(
        conn,
        case_id=case_id,
        visibility=str(row["disclosure_class"]),
        acl_verified=True,
    )
    conn.execute(
        """INSERT OR IGNORE INTO case_sources(
               source_id,case_id,source_type,stable_external_id,title,source_version,
               visibility,requester_access,authority,updated_at,metadata_json)
           VALUES(?,?,'approved_knowledge',?,?,?,?, ?,?,?,?)""",
        (
            source_id,
            case_id,
            knowledge_id,
            row["title"],
            row["content_digest"],
            row["disclosure_class"],
            requester_access,
            row["source_authority"],
            row["updated_at"],
            canonical_json(
                {
                    "content_digest": row["content_digest"],
                    "evidence_layers": row["evidence_layers_json"],
                    "knowledge_id": knowledge_id,
                    "source_digest": row["source_digest"],
                }
            ),
        ),
    )
    conn.execute(
        """INSERT OR IGNORE INTO evidence(
               evidence_id,case_id,source_id,evidence_layer,freshness_at,visibility,
               artifact_hash,claim,result,created_at)
           VALUES(?,?,?,'static',?,?,?,?,?,?)""",
        (
            evidence_id,
            case_id,
            source_id,
            row["updated_at"],
            row["disclosure_class"],
            row["content_digest"],
            f"Approved knowledge {knowledge_id} matches this request",
            row["answer_markdown"],
            now,
        ),
    )
    return evidence_id
