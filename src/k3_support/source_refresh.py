from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import transaction
from .ids import new_id
from .knowledge import register_source
from .lark import CommandResult, LarkError, run_json

Runner = Callable[..., CommandResult]
SUPPORTED_FEISHU_TYPES = {"feishu_doc", "feishu_wiki_docx"}
_LEASE_SECONDS = 120
_BASE_RETRY_SECONDS = 300
_MAX_RETRY_SECONDS = 6 * 3600
_SOURCE_FIELDS = (
    "source_type",
    "stable_external_id",
    "source_version",
    "content_digest",
    "acl_json",
    "url",
    "title",
    "updated_at",
    "last_checked_at",
)


def _document(data: Any) -> dict[str, Any]:
    if isinstance(data, dict) and isinstance(data.get("document"), dict):
        return data["document"]
    if (
        isinstance(data, dict)
        and isinstance(data.get("data"), dict)
        and isinstance(data["data"].get("document"), dict)
    ):
        return data["data"]["document"]
    raise LarkError("document refresh returned no document", error_type="protocol")


def _claim_source(conn: sqlite3.Connection, now: datetime, max_age_hours: int):
    supported = tuple(sorted(SUPPORTED_FEISHU_TYPES))
    placeholders = ",".join("?" for _ in supported)
    with transaction(conn):
        row = conn.execute(
            f"""SELECT sr.*,coalesce(rs.failure_count,0) AS failure_count
                  FROM source_registry sr
                  LEFT JOIN source_refresh_state rs USING(source_id)
                 WHERE sr.source_type IN ({placeholders}) AND sr.url IS NOT NULL
                   AND (sr.last_checked_at IS NULL OR julianday(sr.last_checked_at)<=julianday(?))
                   AND (rs.next_attempt_at IS NULL OR julianday(rs.next_attempt_at)<=julianday(?))
                   AND (rs.lease_expires_at IS NULL OR julianday(rs.lease_expires_at)<=julianday(?))
                 ORDER BY coalesce(rs.last_attempt_at,sr.last_checked_at,''),sr.source_id
                 LIMIT 1""",
            (
                *supported,
                (now - timedelta(hours=max_age_hours)).isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        ).fetchone()
        if row is None:
            return None
        token = new_id("refresh")
        conn.execute(
            """INSERT INTO source_refresh_state(
                   source_id,last_attempt_at,next_attempt_at,last_state,lease_token,lease_expires_at)
               VALUES(?,?,?,'refreshing',?,?)
               ON CONFLICT(source_id) DO UPDATE SET
                   last_attempt_at=excluded.last_attempt_at,last_state='refreshing',
                   lease_token=excluded.lease_token,lease_expires_at=excluded.lease_expires_at""",
            (
                row["source_id"],
                now.isoformat(),
                now.isoformat(),
                token,
                (now + timedelta(seconds=_LEASE_SECONDS)).isoformat(),
            ),
        )
    return dict(row), token


def source_refresh_snapshot(
    conn: sqlite3.Connection, *, now: datetime
) -> dict[str, Any]:
    """Summarize retries without source bodies, URLs or raw provider errors."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='source_refresh_state'"
    ).fetchone():
        return {"available": False}
    rows = conn.execute(
        """SELECT source_id,last_state,failure_count,last_error_type,last_attempt_at,
                  next_attempt_at,lease_expires_at FROM source_refresh_state
              ORDER BY failure_count DESC,last_attempt_at,source_id"""
    ).fetchall()
    now_text = now.astimezone(UTC).isoformat()
    return {
        "available": True,
        "failed_sources": sum(row["failure_count"] > 0 for row in rows),
        "retry_deferred": sum(
            row["failure_count"] > 0 and row["next_attempt_at"] > now_text
            for row in rows
        ),
        "in_flight": sum(
            row["lease_expires_at"] is not None and row["lease_expires_at"] > now_text
            for row in rows
        ),
        "recent_failures": [dict(row) for row in rows if row["failure_count"] > 0][:20],
    }


def refresh_registered_sources(
    conn: sqlite3.Connection,
    *,
    runner: Runner = run_json,
    now: datetime | None = None,
    max_age_hours: int = 24,
    limit: int = 100,
) -> dict[str, Any]:
    """Refresh with durable fair retries, leased writes and source-drift checks."""
    if not 1 <= max_age_hours <= 720:
        raise ValueError("max_age_hours must be between 1 and 720")
    clock = (
        (lambda: now.astimezone(UTC))
        if now is not None
        else (lambda: datetime.now(UTC))
    )
    observed = clock()
    results: list[dict[str, Any]] = []
    for _ in range(max(1, min(limit, 1000))):
        claimed = _claim_source(conn, clock(), max_age_hours)
        if claimed is None:
            break
        row, token = claimed
        source_id = str(row["source_id"])
        result = {"source_id": source_id, "source_type": str(row["source_type"])}
        document = None
        error_type = None
        try:
            response = runner(
                [
                    "docs",
                    "+fetch",
                    "--doc",
                    str(row["url"]),
                    "--doc-format",
                    "markdown",
                    "--detail",
                    "simple",
                    "--as",
                    "user",
                ]
            )
            document = _document(response.data)
            content = document.get("content")
            revision = document.get("revision_id")
            if (
                not isinstance(content, str)
                or not content.strip()
                or isinstance(revision, bool)
                or not isinstance(revision, (str, int))
                or not str(revision).strip()
            ):
                raise LarkError(
                    "document refresh lacks content or revision", error_type="protocol"
                )
            new_digest = hashlib.sha256(content.encode()).hexdigest()
        except (
            LarkError,
            OSError,
            ValueError,
            TypeError,
            subprocess.TimeoutExpired,
        ) as exc:
            error_type = type(exc).__name__

        finished = clock()
        with transaction(conn):
            lease = conn.execute(
                """SELECT 1 FROM source_refresh_state WHERE source_id=? AND lease_token=?
                    AND julianday(lease_expires_at)>julianday(?)""",
                (source_id, token, finished.isoformat()),
            ).fetchone()
            if lease is None:
                results.append({**result, "state": "superseded"})
                continue
            current = conn.execute(
                "SELECT * FROM source_registry WHERE source_id=?", (source_id,)
            ).fetchone()
            changed_while_fetching = current is None or any(
                current[field] != row[field] for field in _SOURCE_FIELDS
            )
            failures = 0
            if changed_while_fetching:
                state = "superseded"
                next_attempt = finished + timedelta(seconds=_BASE_RETRY_SECONDS)
            elif error_type is not None:
                state = "failed"
                failures = int(row["failure_count"]) + 1
                backoff = min(
                    _MAX_RETRY_SECONDS, _BASE_RETRY_SECONDS * 2 ** min(failures - 1, 7)
                )
                next_attempt = finished + timedelta(seconds=backoff)
                result["error_type"] = error_type
            else:
                register_source(
                    conn,
                    source_type=str(row["source_type"]),
                    stable_external_id=str(row["stable_external_id"]),
                    title=row["title"],
                    url=str(row["url"]),
                    acl=json.loads(row["acl_json"]),
                    source_version=str(revision),
                    content_digest=new_digest,
                    updated_at=str(document["updated_at"])
                    if document.get("updated_at")
                    else row["updated_at"],
                    checked_at=finished.isoformat(),
                )
                state = (
                    "changed"
                    if (
                        row["source_version"] != str(revision)
                        or (
                            row["content_digest"] is not None
                            and row["content_digest"] != new_digest
                        )
                    )
                    else "unchanged"
                )
                # Freshness comes from last_checked_at and the caller's age policy;
                # next_attempt_at only delays failed/replaced attempts.
                next_attempt = finished
                result["source_version"] = str(revision)
            conn.execute(
                """UPDATE source_refresh_state
                      SET next_attempt_at=?,failure_count=?,last_error_type=?,last_state=?,
                          lease_token=NULL,lease_expires_at=NULL
                    WHERE source_id=? AND lease_token=?""",
                (
                    next_attempt.isoformat(),
                    failures,
                    error_type if state == "failed" else None,
                    state,
                    source_id,
                    token,
                ),
            )
        results.append({**result, "state": state})

    supported = tuple(sorted(SUPPORTED_FEISHU_TYPES))
    placeholders = ",".join("?" for _ in supported)
    finished = clock()
    overdue = conn.execute(
        f"""SELECT count(*) FROM source_registry
              WHERE source_type IN ({placeholders}) AND url IS NOT NULL
                AND (last_checked_at IS NULL OR julianday(last_checked_at)<=julianday(?))""",
        (*supported, (finished - timedelta(hours=max_age_hours)).isoformat()),
    ).fetchone()[0]
    ignored_unsupported = conn.execute(
        f"SELECT count(*) FROM source_registry WHERE source_type NOT IN ({placeholders}) OR url IS NULL",
        supported,
    ).fetchone()[0]
    return {
        "checked_at": observed.isoformat(),
        "max_age_hours": max_age_hours,
        "selected": len(results),
        "counts": {
            state: sum(item["state"] == state for item in results)
            for state in ("changed", "unchanged", "failed", "unsupported", "superseded")
        },
        "still_overdue": overdue,
        "ignored_unsupported": ignored_unsupported,
        "schedule": source_refresh_snapshot(conn, now=finished),
        "results": results,
    }
