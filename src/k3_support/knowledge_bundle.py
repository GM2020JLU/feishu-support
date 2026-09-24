from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .db import transaction
from .ids import canonical_json, digest
from .knowledge import (
    DISCLOSURE_LEVELS,
    attach_registered_source,
    create_candidate,
    register_source,
    review,
)


class KnowledgeBundleError(ValueError):
    pass


def _approved_entries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in conn.execute(
        """SELECT * FROM knowledge_entries WHERE status='approved'
             ORDER BY project,module,title,knowledge_id"""
    ):
        sources = []
        for source in conn.execute(
            """SELECT ks.source_type,ks.stable_external_id,ks.claim,ks.visibility,
                      sr.title,sr.url,sr.acl_json,sr.source_version,
                      sr.content_digest,sr.updated_at
                 FROM knowledge_sources ks
                 JOIN source_registry sr
                   ON sr.source_type=ks.source_type
                  AND sr.stable_external_id=ks.stable_external_id
                WHERE ks.knowledge_id=?
                ORDER BY ks.source_type,ks.stable_external_id,ks.claim""",
            (row["knowledge_id"],),
        ):
            sources.append(
                {
                    "source_type": source["source_type"],
                    "stable_external_id": source["stable_external_id"],
                    "title": source["title"],
                    "url": source["url"],
                    "acl": json.loads(source["acl_json"]),
                    "source_version": source["source_version"],
                    "content_digest": source["content_digest"],
                    "updated_at": source["updated_at"],
                    "visibility": source["visibility"],
                    "claim": source["claim"],
                }
            )
        if not sources:
            raise KnowledgeBundleError(
                f"approved knowledge has no registered source: {row['knowledge_id']}"
            )
        entries.append(
            {
                "source_knowledge_id": row["knowledge_id"],
                "title": row["title"],
                "questions": json.loads(row["question_variants_json"]),
                "answer_markdown": row["answer_markdown"],
                "project": row["project"],
                "module": row["module"],
                "hardware": row["hardware"],
                "software_version": row["software_version"],
                "applicability": row["applicability"],
                "disclosure_class": row["disclosure_class"],
                "allowed_chat_ids": json.loads(row["allowed_chat_ids_json"]),
                "allowed_user_ids": json.loads(row["allowed_user_ids_json"]),
                "confidence": row["confidence"],
                "source_authority": row["source_authority"],
                "evidence_layers": json.loads(row["evidence_layers_json"]),
                "owner": row["owner"],
                "reviewed_by": row["reviewed_by"],
                "reviewed_at": row["reviewed_at"],
                "review_due_at": row["review_due_at"],
                "source_digest": row["source_digest"],
                "content_digest": row["content_digest"],
                "sources": sources,
            }
        )
    return entries


def export_bundle(
    conn: sqlite3.Connection,
    *,
    output_path: Path,
) -> dict[str, Any]:
    payload = {"schema_version": 1, "entries": _approved_entries(conn)}
    envelope = {**payload, "bundle_digest": digest(payload)}
    output_path = output_path.expanduser()
    if output_path.is_symlink():
        raise KnowledgeBundleError("bundle output must not be a symlink")
    output_path = output_path.resolve()
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, output_path)
    output_path.chmod(0o600)
    return {
        "bundle_digest": envelope["bundle_digest"],
        "entry_count": len(payload["entries"]),
        "output": str(output_path),
    }


def load_bundle(path: Path) -> dict[str, Any]:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise KnowledgeBundleError("bundle must be a regular file")
    path = path.resolve()
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeBundleError("bundle is not valid JSON") from exc
    if not isinstance(envelope, dict) or envelope.get("schema_version") != 1:
        raise KnowledgeBundleError("unsupported bundle schema")
    entries = envelope.get("entries")
    if not isinstance(entries, list) or not entries:
        raise KnowledgeBundleError("bundle has no entries")
    supplied = envelope.get("bundle_digest")
    payload = {"schema_version": 1, "entries": entries}
    calculated = digest(payload)
    if supplied != calculated:
        raise KnowledgeBundleError("bundle digest mismatch")
    required = {
        "source_knowledge_id",
        "title",
        "questions",
        "answer_markdown",
        "project",
        "module",
        "hardware",
        "software_version",
        "applicability",
        "disclosure_class",
        "allowed_chat_ids",
        "allowed_user_ids",
        "confidence",
        "source_authority",
        "evidence_layers",
        "owner",
        "reviewed_by",
        "reviewed_at",
        "review_due_at",
        "source_digest",
        "content_digest",
        "sources",
    }
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != required:
            raise KnowledgeBundleError("bundle entry has invalid fields")
        if not entry["title"] or not entry["answer_markdown"]:
            raise KnowledgeBundleError("bundle entry is missing title or answer")
        if not isinstance(entry["questions"], list) or not entry["questions"]:
            raise KnowledgeBundleError("bundle entry has no question examples")
        if not isinstance(entry["sources"], list) or not entry["sources"]:
            raise KnowledgeBundleError("bundle entry has no sources")
        if entry["disclosure_class"] not in DISCLOSURE_LEVELS:
            raise KnowledgeBundleError("bundle entry has invalid disclosure class")
        expected_content = digest(
            {
                "answer": entry["answer_markdown"],
                "module": entry["module"],
                "project": entry["project"],
                "questions": entry["questions"],
                "version": entry["software_version"],
            }
        )
        if expected_content != entry["content_digest"]:
            raise KnowledgeBundleError("knowledge content digest mismatch")
        source_fields = {
            "source_type",
            "stable_external_id",
            "title",
            "url",
            "acl",
            "source_version",
            "content_digest",
            "updated_at",
            "visibility",
            "claim",
        }
        for source in entry["sources"]:
            if not isinstance(source, dict) or set(source) != source_fields:
                raise KnowledgeBundleError("bundle source has invalid fields")
            if (
                not source["source_type"]
                or not source["stable_external_id"]
                or not source["claim"]
                or not isinstance(source["acl"], dict)
                or source["visibility"] != source["acl"].get("visibility")
            ):
                raise KnowledgeBundleError("bundle source is invalid")
    return envelope


def plan_import(
    conn: sqlite3.Connection,
    *,
    bundle_path: Path,
) -> dict[str, Any]:
    bundle = load_bundle(bundle_path)
    counts = {"create": 0, "approve_candidate": 0, "unchanged": 0}
    for entry in bundle["entries"]:
        existing = conn.execute(
            "SELECT status,content_digest FROM knowledge_entries WHERE source_digest=?",
            (entry["source_digest"],),
        ).fetchone()
        if existing is None:
            counts["create"] += 1
        elif existing["content_digest"] != entry["content_digest"]:
            raise KnowledgeBundleError("target has conflicting source digest")
        elif existing["status"] == "approved":
            counts["unchanged"] += 1
        else:
            counts["approve_candidate"] += 1
    return {
        "bundle_digest": bundle["bundle_digest"],
        "entry_count": len(bundle["entries"]),
        "actions": counts,
    }


def import_bundle(
    conn: sqlite3.Connection,
    *,
    bundle_path: Path,
    approved_digest: str,
    reviewer_id: str,
) -> dict[str, Any]:
    bundle = load_bundle(bundle_path)
    if not reviewer_id.strip():
        raise KnowledgeBundleError("reviewer_id is required")
    if approved_digest != bundle["bundle_digest"]:
        raise KnowledgeBundleError("approved digest does not match bundle")
    plan = plan_import(conn, bundle_path=bundle_path)
    imported: list[str] = []
    for entry in bundle["entries"]:
        for source in entry["sources"]:
            register_source(
                conn,
                source_type=source["source_type"],
                stable_external_id=source["stable_external_id"],
                title=source["title"],
                url=source["url"],
                acl=source["acl"],
                source_version=source["source_version"],
                content_digest=source["content_digest"],
                updated_at=source["updated_at"],
            )
        knowledge_id = create_candidate(
            conn,
            title=entry["title"],
            questions=entry["questions"],
            answer_markdown=entry["answer_markdown"],
            project=entry["project"],
            module=entry["module"],
            software_version=entry["software_version"],
            disclosure_class=entry["disclosure_class"],
            confidence=float(entry["confidence"]),
            source_authority=float(entry["source_authority"]),
            canonical_case_id=None,
            source_digest=entry["source_digest"],
        )
        row = conn.execute(
            "SELECT status,content_digest FROM knowledge_entries WHERE knowledge_id=?",
            (knowledge_id,),
        ).fetchone()
        if row["content_digest"] != entry["content_digest"]:
            raise KnowledgeBundleError("target knowledge content conflicts with bundle")
        if row["status"] == "candidate":
            for source in entry["sources"]:
                attach_registered_source(
                    conn,
                    knowledge_id=knowledge_id,
                    source_type=source["source_type"],
                    stable_external_id=source["stable_external_id"],
                    claim=source["claim"],
                )
        elif row["status"] != "approved":
            expected_sources = {
                (
                    source["source_type"],
                    source["stable_external_id"],
                    source["claim"],
                )
                for source in entry["sources"]
            }
            actual_sources = {
                tuple(source)
                for source in conn.execute(
                    """SELECT source_type,stable_external_id,claim
                         FROM knowledge_sources WHERE knowledge_id=?""",
                    (knowledge_id,),
                )
            }
            if not expected_sources.issubset(actual_sources):
                raise KnowledgeBundleError(
                    "non-candidate target is missing reviewed source mappings"
                )
        if row["status"] != "approved":
            with transaction(conn):
                conn.execute(
                    """UPDATE knowledge_entries SET hardware=?,applicability=?,
                           allowed_chat_ids_json=?,allowed_user_ids_json=?,
                           evidence_layers_json=?,owner=?,review_due_at=?
                       WHERE knowledge_id=?""",
                    (
                        entry["hardware"],
                        entry["applicability"],
                        canonical_json(entry["allowed_chat_ids"]),
                        canonical_json(entry["allowed_user_ids"]),
                        canonical_json(entry["evidence_layers"]),
                        entry["owner"],
                        entry["review_due_at"],
                        knowledge_id,
                    ),
                )
            review(
                conn,
                knowledge_id=knowledge_id,
                reviewer_id=reviewer_id,
                decision="approved",
            )
        imported.append(knowledge_id)
    return {**plan, "knowledge_ids": imported}
