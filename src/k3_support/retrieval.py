from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .db import transaction
from .ids import canonical_json, digest, new_id
from .lark import CommandResult, LarkError, run_json
from .store import EXECUTABLE_CASE_STATES
from .timeutil import iso_now


class RetrievalError(RuntimeError):
    pass


RetrievalRunner = Callable[[list[str]], CommandResult]
_HIGHLIGHT = re.compile(r"</?h[b]?>", re.IGNORECASE)
_SEARCHABLE = re.compile(r"[A-Za-z0-9_.+-]+|[\u3400-\u9fff]{2,}")
_FILLERS = ("请问", "帮忙", "一下", "怎么", "如何", "问题", "排查", "配置")
_CONTEXT_LABEL = re.compile(
    r"^\[消息 [^\]\n]+ / (?:owner|colleague)\]\s*$", re.MULTILINE
)
_COORDINATE = re.compile(r"\b(?:om_|omt_|in_|ctx_)[A-Za-z0-9_-]+\b")


def _require_active_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_owner: str | None,
) -> None:
    """Reject stale work after takeover, pause, cancellation, or lease loss."""
    row = conn.execute(
        """SELECT j.state,j.lease_owner,c.state AS case_state,
                  j.lifecycle_round,c.lifecycle_round AS current_round
             FROM jobs j JOIN cases c USING(case_id) WHERE j.job_id=?""",
        (job_id,),
    ).fetchone()
    if (
        row is None
        or row["state"] != "running"
        or row["lease_owner"] != lease_owner
        or row["case_state"] not in EXECUTABLE_CASE_STATES
        or row["lifecycle_round"] != row["current_round"]
    ):
        raise RetrievalError("retrieval job was cancelled or lost its lease")


def _clean_token(value: str) -> str:
    cleaned = value
    if re.fullmatch(r"[\u3400-\u9fff]+", value):
        for filler in _FILLERS:
            cleaned = cleaned.replace(filler, "")
    return cleaned.strip()


def _search_query(value: str) -> str:
    # A bounded lexical provider query, not AI query planning. Durable input is
    # kept separately in full; context provenance labels are never search terms.
    content = _COORDINATE.sub("", _CONTEXT_LABEL.sub("", value))
    compact = " ".join(content.split())
    tokens = _SEARCHABLE.findall(compact)
    preferred = [
        _clean_token(token)
        for token in tokens
        if token.lower()
        not in {
            "please",
            "help",
            "怎么",
            "如何",
            "请问",
            "帮忙",
            "问题",
            "一下",
            "排查",
        }
    ]
    preferred = [token for token in preferred if token]
    query = " ".join(preferred or tokens or [compact])
    return query[:30].strip()


def retrieval_input_for_case(conn, *, case_id, source_event_pk=None, project=False):
    """Load the actual current Case context, never caller-supplied scope facts.

    project=True is explicit, deterministic DB projection after operator
    controls; it cannot clear pending association, partial input or conflicts.
    """
    from .conversation_context import (
        context_snapshot,
        project_context,
        resolve_event_context,
        validate_context_binding,
    )
    from .knowledge_runtime import event_input_digest

    case = conn.execute(
        "SELECT lifecycle_round FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    if case is None:
        raise RetrievalError("retrieval Case does not exist")
    from .content_retirement import require_case_content, ContentRetiredError
    try:
        require_case_content(conn, case_id=case_id, lifecycle_round=case[0])
    except ContentRetiredError as error:
        raise RetrievalError('retrieval_content_retired') from error
    if source_event_pk is None:
        row = conn.execute(
            "SELECT context_id FROM conversation_contexts WHERE case_id=? AND lifecycle_round=? AND state<>'retired'",
            (case_id, case[0]),
        ).fetchone()
        snapshot = context_snapshot(conn, row[0]) if row else None
    else:
        snapshot = resolve_event_context(conn, source_event_pk)
    if snapshot is None:
        raise RetrievalError("retrieval_context_missing")
    if snapshot["case_id"] != case_id or snapshot["lifecycle_round"] != case[0]:
        raise RetrievalError("retrieval_context_Case_or_round_mismatch")
    if project:
        snapshot = project_context(conn, snapshot["context_id"], snapshot["revision"])
    valid, reason = validate_context_binding(conn, snapshot["binding"])
    if not valid:
        raise RetrievalError(str(reason))
    focus = snapshot["focus_event_pk"]
    if not focus or (source_event_pk is not None and source_event_pk != focus):
        raise RetrievalError("retrieval_source_is_not_current_context_focus")
    event = conn.execute(
        "SELECT * FROM inbound_events WHERE event_pk=?", (focus,)
    ).fetchone()
    if event is None:
        raise RetrievalError("retrieval_source_missing")
    return {
        "kind": "conversation_context",
        "case_id": case_id,
        "lifecycle_round": int(case[0]),
        "context_binding": snapshot["binding"],
        "full_query": snapshot["query"],
        "facts_digest": snapshot["facts_digest"],
        "source_event_pk": focus,
        "source_event_digest": event_input_digest(event),
    }


def _legacy_input(conn, *, case_id, source_event_pk, query, lifecycle_round):
    from .knowledge_runtime import event_input_digest
    from .content_retirement import require_case_content, ContentRetiredError

    try:
        require_case_content(conn, case_id=case_id, lifecycle_round=lifecycle_round)
    except ContentRetiredError as error:
        raise RetrievalError('retrieval_content_retired') from error

    event = conn.execute(
        "SELECT * FROM inbound_events WHERE event_pk=?", (source_event_pk,)
    ).fetchone()
    if event is None:
        raise RetrievalError("retrieval_source_missing")
    is_im = event["source"] in {"feishu_bot_im", "feishu_user_poll"}
    return {
        "kind": "legacy_im_unbound" if is_im else "legacy_non_im",
        "case_id": case_id,
        "lifecycle_round": lifecycle_round,
        "full_query": query,
        "source_event_pk": source_event_pk,
        "source_event_digest": event_input_digest(event),
    }


def _request_digest(case_id, lifecycle_round, value):
    return digest(
        {
            "case_id": case_id,
            "query": value["full_query"],
            "source_event_pk": value["source_event_pk"],
            "identity": "user",
            "lifecycle_round": lifecycle_round,
            "input_binding": value["input_binding"],
            **(
                {"request_generation": value["request_generation"]}
                if value.get("request_generation") is not None
                else {}
            ),
        }
    )


def _stored_input_valid(job, value):
    if (
        value.get("schema_version") != 2
        or not isinstance(value.get("full_query"), str)
        or not isinstance(value.get("input_binding"), dict)
    ):
        return False
    return (
        value.get("query") == _search_query(value["full_query"])
        and value.get("raw_query_digest")
        == hashlib.sha256(value["full_query"].encode()).hexdigest()
        and value.get("input_binding_digest") == digest(value["input_binding"])
        and job["input_digest"]
        == _request_digest(job["case_id"], job["lifecycle_round"], value)
    )


def validate_retrieval_binding(conn, job_or_id, *, require_ai=False):
    """Re-read persisted input for a follow-up; result dictionaries grant nothing."""
    from .conversation_context import validate_context_binding

    job_id = job_or_id if isinstance(job_or_id, str) else dict(job_or_id).get("job_id")
    job = conn.execute(
        "SELECT * FROM jobs WHERE job_id=? AND job_type='retrieve'", (job_id,)
    ).fetchone()
    if job is None:
        return False, "retrieval_job_missing"
    case = conn.execute(
        "SELECT lifecycle_round,state,owner FROM cases WHERE case_id=?",
        (job["case_id"],),
    ).fetchone()
    if case is None or case["lifecycle_round"] != job["lifecycle_round"]:
        return False, "retrieval_Case_round_changed"
    if require_ai and (
        case["owner"] != "hermes" or case["state"] not in EXECUTABLE_CASE_STATES
    ):
        return False, "retrieval_execution_authority_revoked"
    saved = json.loads(job["context_json"])
    binding = saved.get("input_binding")
    try:
        if not isinstance(binding, dict):
            # Old records have no full input attestation. Only actual non-IM
            # sources retain legacy compatibility; a context label cannot fake it.
            legacy = _legacy_input(
                conn,
                case_id=job["case_id"],
                source_event_pk=saved["source_event_pk"],
                query=saved.get("query", ""),
                lifecycle_round=job["lifecycle_round"],
            )
            return (
                (True, None)
                if legacy["kind"] == "legacy_non_im"
                else (False, "retrieval_context_binding_missing")
            )
        if not _stored_input_valid(job, saved):
            return False, "retrieval_input_digest_mismatch"
        if binding.get("kind") == "conversation_context":
            current = retrieval_input_for_case(
                conn, case_id=job["case_id"], source_event_pk=saved["source_event_pk"]
            )
            if (
                current != binding
                or saved.get("full_query") != current["full_query"]
                or saved.get("input_binding_digest") != digest(binding)
            ):
                return False, "retrieval_input_changed"
            return validate_context_binding(
                conn, current["context_binding"], require_ai=require_ai
            )
        legacy = _legacy_input(
            conn,
            case_id=job["case_id"],
            source_event_pk=saved["source_event_pk"],
            query=saved.get("full_query", ""),
            lifecycle_round=job["lifecycle_round"],
        )
        if legacy != binding or saved.get("input_binding_digest") != digest(binding):
            return False, "retrieval_input_changed"
        return (
            (True, None)
            if legacy["kind"] == "legacy_non_im"
            else (False, "retrieval_context_binding_missing")
        )
    except (RetrievalError, KeyError, TypeError, ValueError) as exc:
        return False, str(exc) if isinstance(
            exc, RetrievalError
        ) else "retrieval_input_invalid"


def _fetch_keyword(value: str) -> str:
    tokens = _SEARCHABLE.findall(value)
    safe = [
        _clean_token(re.sub(r"[^A-Za-z0-9_.+\-\u3400-\u9fff]", "", token))
        for token in tokens
    ]
    safe = [token for token in safe if token]
    return "|".join(safe[:8])[:120] or "K3"


def _results(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise RetrievalError("Drive search returned an invalid result envelope")
    return [item for item in value["results"] if isinstance(item, dict)]


def _message_results(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("messages", "items", "results"):
            items = value.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
        return canonical_json(value)
    return ""


def create_retrieval_job(
    conn: sqlite3.Connection,
    config: Config,
    *,
    case_id: str,
    query: str,
    source_event_pk: str,
    request_generation: str | None = None,
    context_binding: dict | None = None,
) -> tuple[str, bool]:
    if request_generation is not None and (
        not isinstance(request_generation, str)
        or not re.fullmatch(r"[a-f0-9]{64}", request_generation)
    ):
        raise RetrievalError("invalid retrieval request generation")
    if not isinstance(query, str) or not query or len(query) > 32768:
        raise RetrievalError("retrieval full query must contain 1..32768 characters")
    normalized = _search_query(query)
    if not normalized:
        raise RetrievalError("retrieval query has no searchable terms")
    case = conn.execute(
        "SELECT lifecycle_round,state,owner FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    if case is None:
        raise RetrievalError("retrieval Case does not exist")
    from .conversation_context import resolve_event_context

    snapshot = resolve_event_context(conn, source_event_pk)
    if snapshot is not None or context_binding is not None:
        bound_input = retrieval_input_for_case(
            conn, case_id=case_id, source_event_pk=source_event_pk
        )
        if query != bound_input["full_query"] or (
            context_binding is not None
            and context_binding != bound_input["context_binding"]
        ):
            raise RetrievalError(
                "retrieval query or context does not match current input"
            )
    else:
        bound_input = _legacy_input(
            conn,
            case_id=case_id,
            source_event_pk=source_event_pk,
            query=query,
            lifecycle_round=case["lifecycle_round"],
        )
    if case["lifecycle_round"] > 1 and (
        case["owner"] != "hermes"
        or case["state"] in {"resolved", "takeover", "cancelled", "paused"}
    ):
        raise RetrievalError("reopened Case requires explicit current-round delegation")
    job_id = new_id("job")
    now = iso_now()
    workdir = config.data_dir / "cases" / case_id / "jobs" / job_id
    context = {
        "schema_version": 2,
        "identity": "user",
        "query": normalized,
        "full_query": query,
        "input_binding": bound_input,
        "input_binding_digest": digest(bound_input),
        "raw_query_digest": hashlib.sha256(query.encode()).hexdigest(),
        "source_event_pk": source_event_pk,
        **(
            {"request_generation": request_generation}
            if request_generation is not None
            else {}
        ),
    }
    input_digest = _request_digest(case_id, case["lifecycle_round"], context)
    with transaction(conn):
        if (
            bound_input["kind"] == "conversation_context"
            and retrieval_input_for_case(
                conn, case_id=case_id, source_event_pk=source_event_pk
            )
            != bound_input
        ):
            raise RetrievalError("retrieval input changed before job creation")
        current = conn.execute(
            "SELECT lifecycle_round,state,owner FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if (
            current is None
            or current["lifecycle_round"] != case["lifecycle_round"]
            or (
                current["lifecycle_round"] > 1
                and (
                    current["owner"] != "hermes"
                    or current["state"]
                    in {"resolved", "takeover", "cancelled", "paused"}
                )
            )
        ):
            raise RetrievalError("Case authority changed before retrieval job creation")
        workdir.mkdir(mode=0o700, parents=True, exist_ok=False)
        workdir.chmod(0o700)
        cursor = conn.execute(
            """INSERT OR IGNORE INTO jobs(job_id,case_id,job_type,state,priority,input_digest,
                   available_at,workdir,context_json,created_at,updated_at)
               VALUES(?,?,'retrieve','queued',50,?,?,?,?,?,?)""",
            (
                job_id,
                case_id,
                input_digest,
                now,
                str(workdir),
                canonical_json(context),
                now,
                now,
            ),
        )
        if cursor.rowcount == 0:
            workdir.rmdir()
            existing = conn.execute(
                "SELECT job_id FROM jobs WHERE case_id=? AND job_type='retrieve' AND input_digest=?",
                (case_id, input_digest),
            ).fetchone()
            return str(existing["job_id"]), False
    return job_id, True


def run_retrieval_job(
    conn: sqlite3.Connection,
    config: Config,
    *,
    job_id: str,
    runner: RetrievalRunner = run_json,
) -> dict[str, Any]:
    job = conn.execute(
        "SELECT * FROM jobs WHERE job_id=? AND job_type='retrieve'", (job_id,)
    ).fetchone()
    if job is None or job["state"] != "running":
        raise RetrievalError("retrieval job is not claimed")
    context = json.loads(job["context_json"])
    required = {
        "identity",
        "query",
        "raw_query_digest",
        "source_event_pk",
    }
    if (
        not required.issubset(context)
        or set(context)
        - required
        - {
            "request_generation",
            "schema_version",
            "full_query",
            "input_binding",
            "input_binding_digest",
        }
        or context["identity"] != "user"
        or (
            "request_generation" in context
            and (
                not isinstance(context["request_generation"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", context["request_generation"])
            )
        )
    ):
        raise RetrievalError("retrieval job context is invalid")
    if context.get("schema_version") is not None and not _stored_input_valid(
        job, context
    ):
        raise RetrievalError(
            "retrieval input digest does not match actual provider query"
        )
    case = conn.execute(
        "SELECT * FROM cases WHERE case_id=?", (job["case_id"],)
    ).fetchone()
    if case is None:
        raise RetrievalError("retrieval Case disappeared")
    now = iso_now()
    with transaction(conn):
        conn.execute(
            """INSERT OR IGNORE INTO job_attempts(attempt_id,job_id,attempt_no,started_at,worker_id)
               VALUES(?,?,?,?,?)""",
            (
                new_id("jat"),
                job_id,
                job["attempt_no"],
                now,
                job["lease_owner"] or "unknown",
            ),
        )
    fetch_errors: list[dict[str, str | None]] = []
    try:
        search = runner(
            [
                "drive",
                "+search",
                "--query",
                context["query"],
                "--page-size",
                "10",
                "--format",
                "json",
                "--as",
                "user",
            ]
        )
        candidates = _results(search.data)
        drive_has_more = (
            bool(search.data.get("has_more"))
            if isinstance(search.data, dict)
            else False
        )
        _require_active_job(conn, job_id=job_id, lease_owner=job["lease_owner"])
    except LarkError as exc:
        candidates = []
        drive_has_more = False
        fetch_errors.append(
            {
                "source": "drive_search",
                "url": None,
                "error_type": exc.error_type,
                "subtype": exc.subtype,
            }
        )
    fetched: list[dict[str, Any]] = []
    for item in candidates:
        meta = item.get("result_meta")
        if not isinstance(meta, dict):
            continue
        url = meta.get("url")
        doc_type = str(meta.get("doc_types") or "").lower()
        if not isinstance(url, str) or doc_type not in {"doc", "docx"}:
            continue
        try:
            document = runner(
                [
                    "docs",
                    "+fetch",
                    "--doc",
                    url,
                    "--scope",
                    "keyword",
                    "--keyword",
                    _fetch_keyword(context["query"]),
                    "--context-before",
                    "1",
                    "--context-after",
                    "1",
                    "--doc-format",
                    "markdown",
                    "--as",
                    "user",
                ]
            )
            _require_active_job(conn, job_id=job_id, lease_owner=job["lease_owner"])
        except LarkError as exc:
            fetch_errors.append(
                {
                    "source": "doc_fetch",
                    "url": url,
                    "error_type": exc.error_type,
                    "subtype": exc.subtype,
                }
            )
            continue
        payload = document.data if isinstance(document.data, dict) else {}
        value = payload.get("document") if isinstance(payload, dict) else None
        if not isinstance(value, dict) or not isinstance(value.get("content"), str):
            continue
        fetched.append(
            {
                "title": _HIGHLIGHT.sub("", str(item.get("title_highlighted") or "")),
                "summary": _HIGHLIGHT.sub(
                    "", str(item.get("summary_highlighted") or "")
                ),
                "url": url,
                "token": meta.get("token"),
                "document_id": value.get("document_id"),
                "revision_id": value.get("revision_id"),
                "update_time": meta.get("update_time_iso"),
                "content": value["content"][:20000],
            }
        )
        if len(fetched) >= 3:
            break
    messages: list[dict[str, Any]] = []
    message_search_count = 0
    try:
        message_search = runner(
            [
                "im",
                "+messages-search",
                "--query",
                context["query"],
                "--page-size",
                "10",
                "--no-reactions",
                "--format",
                "json",
                "--as",
                "user",
            ]
        )
        _require_active_job(conn, job_id=job_id, lease_owner=job["lease_owner"])
        message_candidates = _message_results(message_search.data)
        message_search_count = len(message_candidates)
        for item in message_candidates:
            if item.get("deleted"):
                continue
            message_id = item.get("message_id")
            content = _message_text(item.get("content"))
            if not isinstance(message_id, str) or not content.strip():
                continue
            sender_value = item.get("sender")
            sender: dict[str, Any] = (
                sender_value if isinstance(sender_value, dict) else {}
            )
            messages.append(
                {
                    "message_id": message_id,
                    "chat_id": str(item.get("chat_id") or ""),
                    "chat_type": str(item.get("chat_type") or ""),
                    "chat_name": str(item.get("chat_name") or "")[:300],
                    "sender_id": str(sender.get("id") or sender.get("open_id") or ""),
                    "sender_name": str(sender.get("name") or "")[:300],
                    "create_time": str(item.get("create_time") or ""),
                    "update_time": str(item.get("update_time") or ""),
                    "message_app_link": str(item.get("message_app_link") or "")[:1000],
                    "content": content[:4000],
                }
            )
            if len(messages) >= 5:
                break
    except LarkError as exc:
        fetch_errors.append(
            {
                "source": "message_search",
                "url": None,
                "error_type": exc.error_type,
                "subtype": exc.subtype,
            }
        )
    artifact = {
        "schema_version": 1,
        "query": context["query"],
        "full_query": context.get("full_query"),
        "input_binding": context.get("input_binding"),
        "input_binding_digest": context.get("input_binding_digest"),
        "search_count": len(candidates),
        "has_more": drive_has_more,
        "documents": fetched,
        "message_search_count": message_search_count,
        "messages": messages,
        "fetch_errors": fetch_errors,
    }
    artifact_bytes = canonical_json(artifact).encode()
    _require_active_job(conn, job_id=job_id, lease_owner=job["lease_owner"])
    artifact_path = Path(job["workdir"]) / "retrieval.json"
    if artifact_path.is_symlink() or (
        artifact_path.exists() and not artifact_path.is_file()
    ):
        raise RetrievalError("retrieval artifact path is unsafe")
    temporary = artifact_path.with_name(
        f".retrieval.{int(job['attempt_no'])}.{job_id}.tmp"
    )
    if temporary.exists() or temporary.is_symlink():
        raise RetrievalError("retrieval temporary artifact path is not fresh")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(artifact_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, artifact_path)
    finally:
        temporary.unlink(missing_ok=True)
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()

    source_ids: list[str] = []
    evidence_ids: list[str] = []
    finished = iso_now()
    with transaction(conn):
        _require_active_job(conn, job_id=job_id, lease_owner=job["lease_owner"])
        context_current, current_reason = validate_retrieval_binding(conn, job_id)
        followup_eligible, followup_reason = validate_retrieval_binding(
            conn, job_id, require_ai=True
        )
        for document in fetched:
            stable = str(document["url"])
            source_id = f"src_{digest({'case': job['case_id'], 'url': stable})[:32]}"
            check = {
                "artifact_sha256": artifact_hash,
                "document_id": document["document_id"],
                "query": context["query"],
                "revision_id": document["revision_id"],
                "title": document["title"],
                "update_time": document["update_time"],
                "url": stable,
                "verified": True,
                "retrieval_input_digest": job["input_digest"],
                "retrieval_input_binding_digest": context.get("input_binding_digest"),
            }
            evidence_id = f"evd_{digest({'source_id': source_id, 'check': check})[:32]}"
            conn.execute(
                """INSERT INTO case_sources(source_id,case_id,source_type,stable_external_id,
                       title,source_version,visibility,requester_access,authority,updated_at,
                       metadata_json)
                   VALUES(?,?,'feishu_doc',?,?,?,'internal','unknown',0.8,?,?)
                   ON CONFLICT(case_id,source_type,stable_external_id) DO UPDATE SET
                       source_version=excluded.source_version,updated_at=excluded.updated_at,
                       metadata_json=excluded.metadata_json WHERE ?""",
                (
                    source_id,
                    job["case_id"],
                    stable,
                    document["title"] or stable,
                    str(
                        document["revision_id"] or document["update_time"] or "unknown"
                    ),
                    finished,
                    canonical_json(
                        {
                            "retrieved_as": "user",
                            "retrieval_job_id": job_id,
                            "url": stable,
                        }
                    ),
                    int(context_current),
                ),
            )
            actual_source = conn.execute(
                """SELECT source_id FROM case_sources WHERE case_id=? AND source_type='feishu_doc'
                   AND stable_external_id=?""",
                (job["case_id"], stable),
            ).fetchone()[0]
            conn.execute(
                """INSERT OR IGNORE INTO evidence(evidence_id,case_id,source_id,evidence_layer,
                       freshness_at,visibility,artifact_hash,claim,result,created_at)
                   VALUES(?,?,?,'static',?,'internal',?,?,?,?)""",
                (
                    evidence_id,
                    job["case_id"],
                    actual_source,
                    finished,
                    artifact_hash,
                    f"Retrieved an operator-visible Feishu document for query: {context['query']}",
                    canonical_json(check),
                    finished,
                ),
            )
            source_ids.append(str(actual_source))
            evidence_ids.append(evidence_id)
        for message in messages:
            stable = str(message["message_id"])
            source_id = (
                f"src_{digest({'case': job['case_id'], 'message': stable})[:32]}"
            )
            check = {
                "artifact_sha256": artifact_hash,
                "chat_id": message["chat_id"],
                "create_time": message["create_time"],
                "message_id": stable,
                "query": context["query"],
                "update_time": message["update_time"],
                "verified": True,
                "retrieval_input_digest": job["input_digest"],
                "retrieval_input_binding_digest": context.get("input_binding_digest"),
            }
            evidence_id = f"evd_{digest({'source_id': source_id, 'check': check})[:32]}"
            conn.execute(
                """INSERT INTO case_sources(source_id,case_id,source_type,
                       stable_external_id,title,source_version,visibility,requester_access,
                       authority,updated_at,metadata_json)
                   VALUES(?,?,'feishu_message',?,?,?,'internal','unknown',0.6,?,?)
                   ON CONFLICT(case_id,source_type,stable_external_id) DO UPDATE SET
                       source_version=excluded.source_version,updated_at=excluded.updated_at,
                       metadata_json=excluded.metadata_json WHERE ?""",
                (
                    source_id,
                    job["case_id"],
                    stable,
                    message["chat_name"] or f"Feishu message {stable}",
                    str(message["update_time"] or message["create_time"] or "unknown"),
                    finished,
                    canonical_json(
                        {
                            "chat_id": message["chat_id"],
                            "message_app_link": message["message_app_link"],
                            "retrieval_job_id": job_id,
                            "retrieved_as": "user",
                        }
                    ),
                    int(context_current),
                ),
            )
            actual_source = conn.execute(
                """SELECT source_id FROM case_sources WHERE case_id=?
                   AND source_type='feishu_message' AND stable_external_id=?""",
                (job["case_id"], stable),
            ).fetchone()[0]
            conn.execute(
                """INSERT OR IGNORE INTO evidence(evidence_id,case_id,source_id,
                       evidence_layer,freshness_at,visibility,artifact_hash,claim,result,
                       created_at)
                   VALUES(?,?,?,'static',?,'internal',?,?,?,?)""",
                (
                    evidence_id,
                    job["case_id"],
                    actual_source,
                    finished,
                    artifact_hash,
                    f"Retrieved an operator-visible Feishu message for query: {context['query']}",
                    canonical_json(check),
                    finished,
                ),
            )
            source_ids.append(str(actual_source))
            evidence_ids.append(evidence_id)
        conn.execute(
            """INSERT INTO case_suggestions(suggestion_id,case_id,kind,content_json,confidence,
                   policy_version,status,created_at)
               VALUES(?,?,'next_action',?,0.0,'v1','shadow',?)""",
            (
                new_id("sug"),
                job["case_id"],
                canonical_json(
                    {
                        "action": "review_retrieval_result",
                        "artifact_path": str(artifact_path),
                        "artifact_sha256": artifact_hash,
                        "documents": [
                            {
                                "title": item["title"],
                                "summary": item["summary"],
                                "url": item["url"],
                                "revision_id": item["revision_id"],
                            }
                            for item in fetched
                        ],
                        "messages": [
                            {
                                "message_id": item["message_id"],
                                "chat_name": item["chat_name"],
                                "sender_name": item["sender_name"],
                                "create_time": item["create_time"],
                                "message_app_link": item["message_app_link"],
                            }
                            for item in messages
                        ],
                        "evidence_ids": evidence_ids,
                        "context_current": context_current,
                        "followup_eligible": followup_eligible,
                        "stale_reason": current_reason or followup_reason,
                        "requester_access": "unknown",
                    }
                ),
                finished,
            ),
        )
        succeeded = conn.execute(
            """UPDATE jobs SET state='succeeded',output_digest=?,exit_code=0,lease_owner=NULL,
                   lease_expires_at=NULL,heartbeat_at=?,updated_at=?
                 WHERE job_id=? AND state='running' AND lease_owner=?""",
            (artifact_hash, finished, finished, job_id, job["lease_owner"]),
        )
        if succeeded.rowcount != 1:
            raise RetrievalError("retrieval job lost its lease before completion")
        conn.execute(
            """UPDATE job_attempts SET ended_at=?,result='succeeded',detail_json=?
               WHERE job_id=? AND attempt_no=?""",
            (
                finished,
                canonical_json(
                    {
                        "document_count": len(fetched),
                        "message_count": len(messages),
                        "evidence_ids": evidence_ids,
                    }
                ),
                job_id,
                job["attempt_no"],
            ),
        )
        placeholders = ",".join("?" for _ in EXECUTABLE_CASE_STATES)
        conn.execute(
            f"""UPDATE cases SET next_action=?,updated_at=? WHERE case_id=?
                   AND state IN ({placeholders}) AND ?""",
            (
                (
                    "Review retrieved Feishu evidence and promote a disclosure-safe answer"
                    if fetched or messages
                    else "No Feishu document matched; continue investigation"
                ),
                finished,
                job["case_id"],
                *EXECUTABLE_CASE_STATES,
                int(followup_eligible),
            ),
        )
    return {
        "case_id": job["case_id"],
        "job_id": job_id,
        "query": context["query"],
        "full_query": context.get("full_query"),
        "input_binding_digest": context.get("input_binding_digest"),
        "context_current": context_current,
        "followup_eligible": followup_eligible,
        "stale_reason": current_reason or followup_reason,
        "artifact_path": str(artifact_path),
        "artifact_sha256": artifact_hash,
        "source_ids": source_ids,
        "evidence_ids": evidence_ids,
        "documents": [
            {
                "title": item["title"],
                "url": item["url"],
                "revision_id": item["revision_id"],
            }
            for item in fetched
        ],
        "messages": [
            {
                "message_id": item["message_id"],
                "chat_name": item["chat_name"],
                "sender_name": item["sender_name"],
                "create_time": item["create_time"],
                "message_app_link": item["message_app_link"],
            }
            for item in messages
        ],
        "fetch_errors": fetch_errors,
    }
