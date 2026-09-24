from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import transaction
from .ids import canonical_json, digest
from .timeutil import iso_now

SimilaritySelector = Callable[[dict[str, Any]], dict[str, Any] | None]
DiagnosticExtractor = Callable[[dict[str, Any]], dict[str, Any] | None]
DIAGNOSTIC_FIELDS = {
    "hardware",
    "software_version",
    "boot_stage",
    "boot_media",
    "expected",
    "actual",
    "error_markers",
    "reproduction_steps",
}


def record_diagnostic_snapshot(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    event_pk: str,
    content: str,
    extractor: DiagnosticExtractor | None,
) -> dict[str, Any] | None:
    if extractor is None or not content.strip():
        return None
    source = conn.execute("SELECT payload_json FROM inbound_events WHERE event_pk=?", (event_pk,)).fetchone()
    if source is None:
        return None
    input_digest, source_digest = digest(content), digest(source[0])
    if conn.execute("SELECT 1 FROM diagnostic_snapshots WHERE event_pk=? AND case_id<>?", (event_pk, case_id)).fetchone():
        return None
    existing = conn.execute(
        "SELECT * FROM diagnostic_snapshots WHERE event_pk=? AND input_digest=? AND source_digest=?", (event_pk, input_digest, source_digest)
    ).fetchone()
    if existing is not None:
        if (existing["case_id"] != case_id or existing["input_digest"] != input_digest
                or existing["source_digest"] != source_digest):
            return None
        return {
            "snapshot_id": existing["snapshot_id"],
            "facts": json.loads(existing["facts_json"]),
            "missing": json.loads(existing["missing_json"]),
            "confidence": float(existing["confidence"]),
        }
    raw = extractor(
        {
            "case_id": case_id,
            "message": content,
            "allowed_fact_fields": sorted(DIAGNOSTIC_FIELDS),
        }
    )
    if not isinstance(raw, dict) or set(raw) != {"facts", "missing", "confidence"}:
        return None
    facts = raw["facts"]
    missing = raw["missing"]
    confidence = raw["confidence"]
    if (
        not isinstance(facts, dict)
        or set(facts) - DIAGNOSTIC_FIELDS
        or not all(value is None or isinstance(value, (str, list)) for value in facts.values())
        or not isinstance(missing, list)
        or not all(isinstance(item, str) for item in missing)
        or len(missing) != len(set(missing))
        or not set(missing) <= DIAGNOSTIC_FIELDS
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= float(confidence) <= 1
    ):
        return None
    cleaned: dict[str, Any] = {}
    for key, value in facts.items():
        if isinstance(value, str):
            cleaned[key] = value.strip()[:1000] or None
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            cleaned[key] = [item.strip()[:500] for item in value[:8] if item.strip()]
        elif value is None:
            cleaned[key] = None
        else:
            return None
    snapshot_id = f"dgs_{digest({'case_id': case_id, 'event_pk': event_pk, 'input': input_digest, 'source': source_digest})[:32]}"
    with transaction(conn):
        current_source = conn.execute("SELECT payload_json FROM inbound_events WHERE event_pk=?", (event_pk,)).fetchone()
        if current_source is None or digest(current_source[0]) != source_digest:
            return None
        if conn.execute("SELECT 1 FROM diagnostic_snapshots WHERE event_pk=? AND case_id<>?", (event_pk, case_id)).fetchone():
            return None
        conn.execute(
            """INSERT OR IGNORE INTO diagnostic_snapshots(snapshot_id,case_id,event_pk,
                   facts_json,missing_json,confidence,created_at,input_digest,source_digest) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                snapshot_id, case_id, event_pk, canonical_json(cleaned),
                canonical_json(sorted(missing)), float(confidence), iso_now(), input_digest, source_digest,
            ),
        )
        persisted = conn.execute(
            "SELECT facts_json,missing_json,confidence FROM diagnostic_snapshots WHERE snapshot_id=? AND case_id=? AND event_pk=? AND input_digest=? AND source_digest=?",
            (snapshot_id, case_id, event_pk, input_digest, source_digest),
        ).fetchone()
    if persisted is None:
        return None
    return {
        "snapshot_id": snapshot_id,
        "facts": json.loads(persisted["facts_json"]),
        "missing": json.loads(persisted["missing_json"]),
        "confidence": float(persisted["confidence"]),
    }


def diagnostic_context_records(conn, *, case_id):
    """Return bounded source-current reports, not independently verified facts."""
    rows = conn.execute(
        """SELECT d.*,i.payload_json FROM diagnostic_snapshots d
             JOIN inbound_events i ON i.event_pk=d.event_pk
            WHERE d.case_id=? AND d.rowid=(SELECT max(newer.rowid) FROM diagnostic_snapshots newer WHERE newer.event_pk=d.event_pk)
            ORDER BY d.created_at DESC,d.rowid DESC LIMIT 5""",
        (case_id,),
    ).fetchall()
    return [
        {
            "snapshot_id": row["snapshot_id"],
            "event_pk": row["event_pk"],
            "source_digest": row["source_digest"],
            "reported_at": row["created_at"],
            "facts": json.loads(row["facts_json"]),
            "missing": json.loads(row["missing_json"]),
            "confidence": row["confidence"],
        }
        for row in reversed(rows)
        if row["input_digest"] and row["source_digest"] == digest(row["payload_json"])
    ]


def _candidate_cases(
    conn: sqlite3.Connection, *, case_id: str, now: datetime
) -> list[dict[str, Any]]:
    cutoff = (now - timedelta(days=30)).isoformat()
    rows = conn.execute(
        """SELECT c.case_id,c.title,c.type,c.severity,c.state,c.updated_at,
                  r.domain,r.reason_codes_json
             FROM cases c LEFT JOIN route_decisions r ON r.route_decision_id=(
                 SELECT r2.route_decision_id FROM route_decisions r2
                  WHERE r2.case_id=c.case_id ORDER BY r2.created_at DESC LIMIT 1)
            WHERE c.case_id<>? AND c.type IN ('bug','incident','investigation')
              AND c.created_at>=? AND c.canonical_case_id IS NULL
            ORDER BY c.updated_at DESC LIMIT 40""",
        (case_id, cutoff),
    ).fetchall()
    return [
        {
            "case_id": row["case_id"],
            "title": row["title"],
            "type": row["type"],
            "severity": row["severity"],
            "state": row["state"],
            "domain": row["domain"],
            "reason_codes": json.loads(row["reason_codes_json"] or "[]"),
        }
        for row in rows
    ]


def attach_similar_case(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    query: str,
    selector: SimilaritySelector | None,
    minimum_confidence: float = 0.9,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    case = conn.execute(
        "SELECT type,title FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    if case is None or case["type"] not in {"bug", "incident", "investigation"}:
        return None
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    candidates = _candidate_cases(conn, case_id=case_id, now=observed)
    if not candidates or selector is None:
        return None
    raw = selector(
        {
            "case": {"case_id": case_id, "title": case["title"], "message": query},
            "candidates": candidates,
        }
    )
    if not isinstance(raw, dict) or set(raw) != {
        "canonical_case_id", "confidence", "reason"
    }:
        return None
    canonical_id = raw["canonical_case_id"]
    confidence = raw["confidence"]
    reason = raw["reason"]
    allowed = {str(item["case_id"]) for item in candidates}
    if canonical_id is None:
        return None
    if (
        not isinstance(canonical_id, str)
        or canonical_id not in allowed
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not minimum_confidence <= float(confidence) <= 1
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        return None
    now_text = iso_now()
    with transaction(conn):
        membership = conn.execute(
            "SELECT cluster_id FROM incident_cluster_members WHERE case_id=?",
            (canonical_id,),
        ).fetchone()
        if membership:
            cluster_id = str(membership["cluster_id"])
        else:
            cluster_id = f"icl_{digest({'canonical_case_id': canonical_id})[:32]}"
            conn.execute(
                """INSERT OR IGNORE INTO incident_clusters(cluster_id,canonical_case_id,state,
                       created_at,updated_at) VALUES(?,?,'open',?,?)""",
                (cluster_id, canonical_id, now_text, now_text),
            )
            conn.execute(
                """INSERT OR IGNORE INTO incident_cluster_members(cluster_id,case_id,
                       similarity,reason,created_at) VALUES(?,?,1.0,'canonical',?)""",
                (cluster_id, canonical_id, now_text),
            )
        conn.execute(
            """INSERT OR IGNORE INTO incident_cluster_members(cluster_id,case_id,similarity,
                   reason,created_at) VALUES(?,?,?,?,?)""",
            (cluster_id, case_id, float(confidence), reason.strip()[:1000], now_text),
        )
        conn.execute(
            "UPDATE incident_clusters SET updated_at=? WHERE cluster_id=?",
            (now_text, cluster_id),
        )
    return {
        "cluster_id": cluster_id,
        "canonical_case_id": canonical_id,
        "case_id": case_id,
        "similarity": float(confidence),
    }
