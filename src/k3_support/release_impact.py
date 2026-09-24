from __future__ import annotations

import html
import json
import re
import sqlite3
from collections.abc import Callable
from typing import Any

from .config import Config
from .db import transaction
from .ids import canonical_json, digest, new_id
from .operator_notifications import enqueue as enqueue_notice
from .timeutil import iso_now


class ReleaseImpactError(ValueError):
    pass


_REVISION = re.compile(r"[0-9a-fA-F]{7,64}")
_LEVELS = {"low", "medium", "high"}


def _short_list(value: Any, *, name: str, limit: int, size: int) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ReleaseImpactError(f"{name} must be a bounded list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > size:
            raise ReleaseImpactError(f"{name} contains an invalid item")
        result.append(" ".join(item.split()))
    return result


def normalize_change(config: Config, value: dict[str, Any]) -> dict[str, Any]:
    required = {"repository", "change_id", "revision", "subject", "branch", "changed_paths"}
    if not isinstance(value, dict) or set(value) != required:
        raise ReleaseImpactError("change keys do not match schema")
    repository = value["repository"]
    if not isinstance(repository, str) or repository not in config.raw["repositories"]:
        raise ReleaseImpactError("repository is not configured")
    change_id = value["change_id"]
    if not isinstance(change_id, str) or not change_id.strip() or len(change_id) > 100:
        raise ReleaseImpactError("change_id is invalid")
    revision = value["revision"]
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise ReleaseImpactError("revision must be an exact hexadecimal revision")
    subject = value["subject"]
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 300:
        raise ReleaseImpactError("subject is invalid")
    branch = value["branch"]
    if branch is not None and (not isinstance(branch, str) or not branch.strip() or len(branch) > 200):
        raise ReleaseImpactError("branch is invalid")
    paths = _short_list(value["changed_paths"], name="changed_paths", limit=500, size=500)
    if any(path.startswith("/") or ".." in path.split("/") or "\x00" in path for path in paths):
        raise ReleaseImpactError("changed_paths must be repository-relative")
    return {
        "repository": repository,
        "change_id": change_id.strip(),
        "revision": revision.lower(),
        "subject": " ".join(subject.split()),
        "branch": branch.strip() if isinstance(branch, str) else None,
        "changed_paths": paths,
    }


def _knowledge_catalog(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "knowledge_id": str(row["knowledge_id"]),
            "title": str(row["title"]),
            "project": row["project"],
            "module": row["module"],
            "software_version": row["software_version"],
            "status": row["status"],
        }
        for row in conn.execute(
            """SELECT knowledge_id,title,project,module,software_version,status
                 FROM knowledge_entries WHERE status IN ('approved','stale')
                 ORDER BY knowledge_id LIMIT 1000"""
        )
    ]


def _source_change_matches(
    conn: sqlite3.Connection, *, repository: str, changed_paths: list[str]
) -> list[dict[str, str]]:
    """Match exact Git source locators; AI output is deliberately not consulted."""
    changed = set(changed_paths)
    matches: list[dict[str, str]] = []
    rows = conn.execute(
        """SELECT pkr.revision_id,pkr.stable_id,pkr.lifecycle_state,pkr.knowledge_id,
                  pkc.claim_id,pkc.local_claim_id,pcs.source_id,pcs.locator_json
             FROM professional_knowledge_revisions pkr
             JOIN knowledge_entries ke ON ke.professional_revision_id=pkr.revision_id
             JOIN professional_knowledge_claims pkc ON pkc.revision_id=pkr.revision_id
             JOIN professional_claim_sources pcs ON pcs.claim_id=pkc.claim_id
            WHERE pkr.lifecycle_state IN ('published','needs_review')
              AND ke.status IN ('approved','stale')
              AND pcs.source_type='git'
            ORDER BY pkr.stable_id,pkc.local_claim_id,pcs.source_id"""
    )
    for row in rows:
        try:
            locator = json.loads(row["locator_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(locator, dict) or locator.get("repository") != repository:
            continue
        path = locator.get("path")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or ".." in path.split("/")
            or path not in changed
        ):
            continue
        matches.append(
            {
                "revision_id": str(row["revision_id"]),
                "stable_id": str(row["stable_id"]),
                "knowledge_id": str(row["knowledge_id"]),
                "claim_id": str(row["claim_id"]),
                "local_claim_id": str(row["local_claim_id"]),
                "source_id": str(row["source_id"]),
                "changed_path": path,
                "previous_lifecycle_state": str(row["lifecycle_state"]),
            }
        )
    return matches


def invalidate_professional_knowledge(
    conn: sqlite3.Connection,
    *,
    change: dict[str, Any],
    input_digest: str,
) -> list[dict[str, str]]:
    """Persist deterministic Claim-level staleness inside the caller's transaction."""
    matches = _source_change_matches(
        conn,
        repository=str(change["repository"]),
        changed_paths=list(change["changed_paths"]),
    )
    created_at = iso_now()
    invalidated: list[dict[str, str]] = []
    for match in matches:
        event_id = "kse_" + digest(
            {
                "input_digest": input_digest,
                "revision_id": match["revision_id"],
                "claim_id": match["claim_id"],
                "source_id": match["source_id"],
                "changed_path": match["changed_path"],
            }
        )[:32]
        cursor = conn.execute(
            """INSERT OR IGNORE INTO professional_source_change_events(
                   event_id,input_digest,repository,change_id,revision,changed_path,
                   knowledge_revision_id,claim_id,source_id,previous_lifecycle_state,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                input_digest,
                change["repository"],
                change["change_id"],
                change["revision"],
                match["changed_path"],
                match["revision_id"],
                match["claim_id"],
                match["source_id"],
                match["previous_lifecycle_state"],
                created_at,
            ),
        )
        if cursor.rowcount == 0:
            continue
        conn.execute(
            """UPDATE professional_knowledge_revisions
                  SET lifecycle_state='needs_review' WHERE revision_id=?""",
            (match["revision_id"],),
        )
        conn.execute(
            """UPDATE knowledge_entries SET status='stale',updated_at=?
                 WHERE knowledge_id=? AND professional_revision_id=?""",
            (created_at, match["knowledge_id"], match["revision_id"]),
        )
        invalidated.append({**match, "event_id": event_id})
    return invalidated


def _existing_invalidations(
    conn: sqlite3.Connection, input_digest: str
) -> list[dict[str, str]]:
    return [
        dict(row)
        for row in conn.execute(
            """SELECT e.event_id,pkr.stable_id,e.knowledge_revision_id AS revision_id,
                      e.claim_id,pkc.local_claim_id,e.source_id,e.changed_path
                 FROM professional_source_change_events e
                 JOIN professional_knowledge_revisions pkr
                   ON pkr.revision_id=e.knowledge_revision_id
                 JOIN professional_knowledge_claims pkc ON pkc.claim_id=e.claim_id
                WHERE e.input_digest=? ORDER BY pkr.stable_id,pkc.local_claim_id""",
            (input_digest,),
        )
    ]


def _validate_assessment(raw: Any, allowed_ids: set[str]) -> dict[str, Any]:
    fields = {
        "summary",
        "impact_level",
        "affected_knowledge_ids",
        "risks",
        "likely_questions",
        "recommended_validation",
        "confidence",
    }
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ReleaseImpactError("assessment keys do not match schema")
    summary = raw["summary"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 1000:
        raise ReleaseImpactError("assessment summary is invalid")
    level = raw["impact_level"]
    if level not in _LEVELS:
        raise ReleaseImpactError("assessment impact_level is invalid")
    affected = _short_list(
        raw["affected_knowledge_ids"], name="affected_knowledge_ids", limit=100, size=100
    )
    if len(affected) != len(set(affected)) or any(item not in allowed_ids for item in affected):
        raise ReleaseImpactError("assessment invented a knowledge ID")
    confidence = raw["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ReleaseImpactError("assessment confidence is invalid")
    return {
        "summary": " ".join(summary.split()),
        "impact_level": level,
        "affected_knowledge_ids": affected,
        "risks": _short_list(raw["risks"], name="risks", limit=10, size=500),
        "likely_questions": _short_list(
            raw["likely_questions"], name="likely_questions", limit=10, size=500
        ),
        "recommended_validation": _short_list(
            raw["recommended_validation"], name="recommended_validation", limit=10, size=500
        ),
        "confidence": float(confidence),
    }


def _render(
    change: dict[str, Any], assessment: dict[str, Any], invalidated: list[dict[str, str]]
) -> str:
    labels = {"low": "低", "medium": "中", "high": "高"}
    lines = [
        "<b>版本变更影响提醒</b>",
        "",
        f"<b>仓库：</b>{html.escape(change['repository'])}",
        (
            f"<b>变更：</b><code>{html.escape(change['change_id'])}</code> · "
            f"<code>{html.escape(change['revision'][:12])}</code>"
        ),
        f"<b>主题：</b>{html.escape(change['subject'])}",
        (
            f"<b>影响：</b>{labels[assessment['impact_level']]} · "
            f"{html.escape(assessment['summary'])}"
        ),
    ]
    if assessment["affected_knowledge_ids"]:
        lines.append("<b>需复核知识：</b>" + ", ".join(
            f"<code>{html.escape(item)}</code>" for item in assessment["affected_knowledge_ids"]
        ))
    if assessment["risks"]:
        lines.append("<b>风险：</b>" + "；".join(html.escape(item) for item in assessment["risks"][:3]))
    if assessment["recommended_validation"]:
        lines.append(
            "<b>建议验证：</b>" + "；".join(
                html.escape(item) for item in assessment["recommended_validation"][:3]
            )
        )
    if invalidated:
        stable_ids = sorted({item["stable_id"] for item in invalidated})
        lines.append(
            "<b>已自动下线：</b>"
            + ", ".join(f"<code>{html.escape(item)}</code>" for item in stable_ids)
            + "（精确源码路径命中，复核后才能恢复）"
        )
    lines.append("仅提醒你；不会自动通知同事。")
    return "\n".join(lines)


def assess_release_change(
    conn: sqlite3.Connection,
    config: Config,
    *,
    change: dict[str, Any],
    analyzer: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
) -> dict[str, Any]:
    """Persist one exact revision and notify the operator once after strict AI validation."""
    normalized = normalize_change(config, change)
    input_digest = digest(normalized)
    existing = conn.execute(
        "SELECT impact_id,outbox_id,assessment_json FROM release_impacts WHERE input_digest=?",
        (input_digest,),
    ).fetchone()
    if existing is not None:
        return {
            "impact_id": str(existing["impact_id"]),
            "outbox_id": existing["outbox_id"],
            "assessment": json.loads(existing["assessment_json"]),
            "invalidated_claims": _existing_invalidations(conn, input_digest),
            "created": False,
        }
    catalog = _knowledge_catalog(conn)
    # Source-path invalidation is deterministic and safety-critical.  Persist it
    # before the optional AI explanation so an analyzer outage cannot leave a
    # known-stale answer eligible for automatic reply.
    with transaction(conn):
        invalidate_professional_knowledge(
            conn, change=normalized, input_digest=input_digest
        )
    invalidated = _existing_invalidations(conn, input_digest)
    if analyzer is None:
        raise ReleaseImpactError("AI impact analyzer is unavailable")
    raw = analyzer({"change": normalized, "approved_knowledge": catalog})
    assessment = _validate_assessment(raw, {item["knowledge_id"] for item in catalog})
    impact_id = new_id("imp")
    with transaction(conn):
        outbox_id, _ = enqueue_notice(
            conn, config,
            action_type="release_impact",
            payload={
                "text": _render(normalized, assessment, invalidated),
                "parse_mode": "HTML",
            },
            idempotency_key=f"release-impact:{input_digest}",
        )
        conn.execute(
            """INSERT INTO release_impacts(
                   impact_id,repository,change_id,revision,subject,branch,
                   changed_paths_json,input_digest,assessment_json,outbox_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                impact_id,
                normalized["repository"],
                normalized["change_id"],
                normalized["revision"],
                normalized["subject"],
                normalized["branch"],
                canonical_json(normalized["changed_paths"]),
                input_digest,
                canonical_json(assessment),
                outbox_id,
                iso_now(),
            ),
        )
    return {
        "impact_id": impact_id,
        "outbox_id": outbox_id,
        "assessment": assessment,
        "invalidated_claims": invalidated,
        "created": True,
    }
