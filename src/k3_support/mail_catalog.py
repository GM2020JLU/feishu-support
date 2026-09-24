from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from collections.abc import Callable
from typing import Any

from .config import Config
from .db import transaction
from .ids import canonical_json, new_id, digest
from .pagination import PaginationError, decode_page
from .lark import CommandResult, run_mail_json
from .timeutil import iso_now


class MailCatalogError(RuntimeError):
    pass


class MailCatalogSchemaError(MailCatalogError):
    pass


class MailCatalogValueError(MailCatalogError):
    pass


class MailCatalogStale(MailCatalogError):
    pass


def prune_cursor_history(conn, *, limit=256):
    """Only completed or explicitly superseded runs; never a resumable failure."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise MailCatalogError('invalid cursor cleanup batch size')
    cutoff = (datetime.now(UTC)-timedelta(days=30)).isoformat()
    with transaction(conn):
        return conn.execute('''DELETE FROM mail_catalog_cursors WHERE rowid IN (
            SELECT h.rowid FROM mail_catalog_cursors h JOIN mail_catalog_runs r USING(run_id)
            WHERE r.updated_at<? AND (r.state='complete' OR (r.state='failed' AND r.last_error='explicit_restart'))
            ORDER BY r.updated_at,h.rowid LIMIT ?)''', (cutoff, limit)).rowcount


CATEGORIES = {
    "build_ci",
    "code_review",
    "upstream",
    "company",
    "project_release",
    "support_bug",
    "meeting",
    "security_account",
    "external",
    "other",
}
ORIGINS = {"automation", "internal_human", "upstream", "external", "unknown"}
ATTENTION = {"action_required", "waiting", "blocked", "information"}
SINGLETON_CLASSIFIER_ATTEMPTS = 3
TOPICS = {
    "bootloader",
    "ufs",
    "ec",
    "k3_platform",
    "linux_kernel",
    "build_infra",
    "project",
    "company",
    "other",
}
FOLDERS = ("INBOX", "ARCHIVED")


def _data(result: CommandResult) -> dict[str, Any]:
    if not isinstance(result.data, dict):
        raise MailCatalogError("mail catalog command returned no object")
    return result.data


def _sender(value: Any) -> tuple[str | None, str | None]:
    text = str(value or "").strip()
    match = re.match(r"^(.*?)\s*<([^<>]+@[^<>]+)>$", text)
    if match:
        return match.group(1).strip() or None, match.group(2).strip().lower()
    return (None, text.lower()) if "@" in text else (text or None, None)


def _classification_input(summary: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    sender = detail.get("head_from") or {}
    sender_name, sender_address = _sender(summary.get("from"))
    return {
        "message_id": str(summary["message_id"]),
        "thread_id": summary.get("thread_id") or detail.get("thread_id"),
        "folder": summary.get("folder"),
        "sender_name": sender.get("name") or sender_name,
        "sender_address": sender.get("mail_address") or sender_address,
        "subject": detail.get("subject") or summary.get("subject"),
        "body_excerpt": str(
            detail.get("body_plain_text") or detail.get("body_preview") or ""
        )[:800],
        "internal_date": detail.get("internal_date") or summary.get("date"),
        "labels": detail.get("label_ids") or summary.get("labels") or [],
    }


def _validate_classifications(
    raw: Any, *, expected_ids: set[str]
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or set(raw) != {"items"} or not isinstance(raw["items"], list):
        raise MailCatalogSchemaError("mail classifier returned no valid items schema")
    result: dict[str, dict[str, Any]] = {}
    required = {"message_id", "category", "origin", "attention", "topics", "confidence"}
    for item in raw["items"]:
        if not isinstance(item, dict) or set(item) != required:
            raise MailCatalogSchemaError("mail classification item schema is invalid")
        message_id = item["message_id"]
        topics = item["topics"]
        confidence = item["confidence"]
        if (
            not isinstance(message_id, str)
            or message_id in result
            or item["category"] not in CATEGORIES
            or item["origin"] not in ORIGINS
            or item["attention"] not in ATTENTION
            or not isinstance(topics, list)
            or len(topics) > 5
            or len(topics) != len(set(topics))
            or any(topic not in TOPICS for topic in topics)
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise MailCatalogValueError("mail classification value is invalid")
        result[message_id] = {
            **item,
            "confidence": float(confidence),
        }
    if set(result) != expected_ids:
        raise MailCatalogValueError(
            "mail classifier did not return every exact message ID"
        )
    return result


def _run_row(
    conn: sqlite3.Connection, *, page_size: int, restart: bool, resume_failed: bool = False
) -> sqlite3.Row:
    if restart and resume_failed:
        raise MailCatalogError('restart and resume_failed are mutually exclusive')
    with transaction(conn):
        row = conn.execute('SELECT * FROM mail_catalog_runs ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
        if row is not None and not restart:
            if row['state'] == 'failed' and resume_failed:
                conn.execute("UPDATE mail_catalog_runs SET state='running',revision=revision+1,last_error=NULL,error_json=NULL,updated_at=? WHERE run_id=? AND state='failed'", (iso_now(), row['run_id']))
                row = conn.execute('SELECT * FROM mail_catalog_runs WHERE run_id=?', (row['run_id'],)).fetchone()
            if (row['state'] == 'running' and row['pages_processed'] > 0 and row['next_page_token']
                    and not conn.execute('SELECT 1 FROM mail_catalog_cursors WHERE run_id=? LIMIT 1', (row['run_id'],)).fetchone()):
                # Older schemas retained only this checkpoint. Record only what
                # is known; never fabricate the chain that led to it.
                conn.execute('INSERT INTO mail_catalog_cursors VALUES(?,?,?,?)',
                             (row['run_id'], row['folder_index'], digest(row['next_page_token']), iso_now()))
            return row
        if row is not None and row['state'] == 'running':
            conn.execute("UPDATE mail_catalog_runs SET state='failed',revision=revision+1,last_error='explicit_restart',error_json=?,updated_at=? WHERE run_id=?", (canonical_json({'reason': 'explicit_restart'}), iso_now(), row['run_id']))
        run_id = new_id('mcr')
        now = iso_now()
        conn.execute(
            """INSERT INTO mail_catalog_runs(
                   run_id,folders_json,page_size,state,created_at,updated_at)
               VALUES(?,?,?,'running',?,?)""",
            (run_id, canonical_json(list(FOLDERS)), page_size, now, now),
        )
    return conn.execute("SELECT * FROM mail_catalog_runs WHERE run_id=?", (run_id,)).fetchone()


def _store_item(
    conn: sqlite3.Connection,
    value: dict[str, Any],
    item: dict[str, Any],
    *,
    now: str,
) -> None:
    labels = value["labels"]
    if isinstance(labels, str):
        labels = [part.strip() for part in labels.split(",") if part.strip()]
    conn.execute(
        """INSERT INTO mail_catalog_items(
               message_id,thread_id,folder,sender_name,sender_address,
               subject,internal_date,labels_json,category,origin,attention,
               topics_json,confidence,classification_source,first_seen_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(message_id) DO UPDATE SET
               thread_id=excluded.thread_id,folder=excluded.folder,
               sender_name=excluded.sender_name,sender_address=excluded.sender_address,
               subject=excluded.subject,internal_date=excluded.internal_date,
               labels_json=excluded.labels_json,category=excluded.category,
               origin=excluded.origin,attention=excluded.attention,
               topics_json=excluded.topics_json,confidence=excluded.confidence,
               classification_source=excluded.classification_source,
               updated_at=excluded.updated_at""",
        (
            value["message_id"],
            value["thread_id"],
            value["folder"],
            value["sender_name"],
            value["sender_address"],
            value["subject"],
            value["internal_date"],
            canonical_json(labels or []),
            item["category"],
            item["origin"],
            item["attention"],
            canonical_json(item["topics"]),
            item["confidence"],
            item.get("_source", "ai_v1"),
            now,
            now,
        ),
    )
    # A scanner may refresh metadata or retry AI classification, but must not
    # overwrite the operator's latest explicit category correction.
    correction = conn.execute(
        """SELECT category FROM mail_category_corrections WHERE message_id=?
             ORDER BY created_at DESC,rowid DESC LIMIT 1""", (value["message_id"],)
    ).fetchone()
    if correction:
        conn.execute(
            "UPDATE mail_catalog_items SET category=?,classification_source='operator' WHERE message_id=?",
            (correction["category"], value["message_id"]),
        )


def _classify_batches(
    inputs: list[dict[str, Any]],
    classifier: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> dict[str, dict[str, Any]]:
    classifications: dict[str, dict[str, Any]] = {}
    ambiguous = []
    for value in inputs:
        classified = _deterministic_classification(value)
        if classified is None:
            ambiguous.append(value)
        else:
            classifications[value["message_id"]] = classified

    def classify_exact(batch: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        raw = classifier(
            {
                "categories": sorted(CATEGORIES),
                "origins": sorted(ORIGINS),
                "attention": sorted(ATTENTION),
                "topics": sorted(TOPICS),
                "messages": batch,
            }
        )
        if (
            len(batch) == 1
            and isinstance(raw, dict)
            and set(raw) == {"items"}
            and isinstance(raw["items"], list)
            and len(raw["items"]) == 1
            and isinstance(raw["items"][0], dict)
        ):
            # The caller, not the model, owns the one-to-one association for a
            # singleton recovery request. Normalize the opaque ID before the
            # same strict schema/value validation used for ordinary batches.
            normalized_item = dict(raw["items"][0])
            normalized_item["message_id"] = batch[0]["message_id"]
            raw = {"items": [normalized_item]}
        return _validate_classifications(
            raw, expected_ids={item["message_id"] for item in batch}
        )

    def unresolved(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "message_id": value["message_id"],
            "category": "other",
            "origin": "unknown",
            "attention": "action_required",
            "topics": ["other"],
            "confidence": 0.0,
            "_source": "ai_unresolved_v1",
        }

    def classify_singletons(
        batch: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        for value in batch:
            last_error: MailCatalogError | None = None
            for _attempt in range(SINGLETON_CLASSIFIER_ATTEMPTS):
                try:
                    values.update(classify_exact([value]))
                    break
                except MailCatalogError as exc:
                    last_error = exc
            else:
                assert last_error is not None
                if not isinstance(last_error, MailCatalogSchemaError):
                    raise last_error
                values[value["message_id"]] = unresolved(value)
        return values

    for offset in range(0, len(ambiguous), 10):
        batch = ambiguous[offset : offset + 10]
        try:
            values = classify_exact(batch)
            source = "ai_v1"
        except MailCatalogValueError:
            # Models occasionally omit one opaque message ID from an otherwise
            # valid batch. Retry each exact item independently and bind its ID
            # in the program rather than trusting the model to copy it.
            values = classify_singletons(batch)
            source = "ai_retry_v1"
        except MailCatalogSchemaError:
            # A model/provider can occasionally return no JSON at all. Retry
            # the whole bounded batch twice more. If all three attempts have no
            # usable schema, surface every item for review rather than issuing
            # up to thirty singleton calls or blocking the mailbox forever.
            source = "ai_batch_retry_v1"
            for _attempt in range(1, SINGLETON_CLASSIFIER_ATTEMPTS):
                try:
                    values = classify_exact(batch)
                    break
                except MailCatalogValueError:
                    values = classify_singletons(batch)
                    source = "ai_retry_v1"
                    break
                except MailCatalogSchemaError:
                    continue
            else:
                values = {value["message_id"]: unresolved(value) for value in batch}
        for item in values.values():
            item.setdefault("_source", source)
        classifications.update(values)
    return classifications


def _deterministic_classification(value: dict[str, Any]) -> dict[str, Any] | None:
    subject = str(value.get("subject") or "")
    subject_lower = subject.lower()
    sender = " ".join(
        str(value.get(key) or "") for key in ("sender_name", "sender_address")
    )
    body = str(value.get("body_excerpt") or "")
    text = f"{sender}\n{subject}\n{body}".lower()
    topics = []
    if any(term in text for term in ("u-boot", "uboot", "edk2", "opensbi", "spl", "esos")):
        topics.append("bootloader")
    if "ufs" in text:
        topics.append("ufs")
    if any(term in text for term in ("embedded controller", " ec ", "ec:", "crosec", "ectool")):
        topics.append("ec")
    if "k3" in text:
        topics.append("k3_platform")
    if any(term in text for term in ("linux", "kernel", "drivers/", "riscv")):
        topics.append("linux_kernel")
    if not topics:
        topics = ["other"]
    else:
        topics = list(dict.fromkeys(topics))[:5]
    failed = any(
        term in text
        for term in (
            "build failed",
            "build aborted",
            "build was aborted",
            "verified -1",
            "pipeline failed",
        )
    )
    build = any(
        term in text
        for term in (
            "build started",
            "build successful",
            "build succeeded",
            "build failed",
            "build aborted",
            "verified +1",
            "verified -1",
            "pipeline ",
        )
    )
    is_gerrit = "gerrit" in sender.lower() or "codereview" in sender.lower()
    if is_gerrit and build:
        return {
            "message_id": value["message_id"],
            "category": "build_ci",
            "origin": "automation",
            "attention": "blocked" if failed else "information",
            "topics": list(dict.fromkeys([*topics, "build_infra"]))[:5],
            "confidence": 0.99,
            "_source": "rules_v1",
        }
    if is_gerrit and ("change in ..." in subject_lower or "gerrit" in text):
        return {
            "message_id": value["message_id"],
            "category": "code_review",
            "origin": "automation",
            "attention": "information",
            "topics": topics,
            "confidence": 0.98,
            "_source": "rules_v1",
        }
    if re.search(r"\[(?:rfc\s+)?(?:patch|bug report)\b", subject_lower):
        return {
            "message_id": value["message_id"],
            "category": "upstream",
            "origin": "upstream",
            "attention": "information",
            "topics": topics,
            "confidence": 0.98,
            "_source": "rules_v1",
        }
    return None


def _reclassify_unresolved(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT * FROM mail_catalog_items WHERE classification_source='ai_unresolved_v1'"
    ).fetchall()
    replacements: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        value = {
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "folder": row["folder"],
            "sender_name": row["sender_name"],
            "sender_address": row["sender_address"],
            "subject": row["subject"],
            "body_excerpt": "",
            "internal_date": row["internal_date"],
            "labels": json.loads(row["labels_json"]),
        }
        classified = _deterministic_classification(value)
        if classified is not None:
            replacements.append((value, classified))
    if not replacements:
        return 0
    now = iso_now()
    with transaction(conn):
        for value, classified in replacements:
            _store_item(conn, value, classified, now=now)
    return len(replacements)


def _index_pending_inbound(
    conn: sqlite3.Connection,
    classifier: Callable[[dict[str, Any]], dict[str, Any] | None],
    *,
    limit: int,
) -> int:
    rows = conn.execute(
        """SELECT ie.payload_json FROM inbound_events ie
             LEFT JOIN mail_catalog_items mc
               ON mc.message_id=substr(ie.external_id,1,length(ie.external_id)-9)
            WHERE ie.source='feishu_mail' AND ie.external_id LIKE '%:received'
              AND mc.message_id IS NULL
            ORDER BY ie.received_epoch LIMIT ?""",
        (limit,),
    ).fetchall()
    inputs = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        message_id = str(payload.get("message_id") or "")
        if not message_id:
            raise MailCatalogError("inbound mail is missing message_id")
        inputs.append(
            _classification_input(
                {
                    "message_id": message_id,
                    "thread_id": payload.get("thread_id"),
                    "folder": payload.get("folder_id"),
                    "from": "",
                    "subject": payload.get("subject"),
                    "date": payload.get("internal_date"),
                    "labels": payload.get("label_ids") or [],
                },
                payload,
            )
        )
    if not inputs:
        return 0
    classifications = _classify_batches(inputs, classifier)
    with transaction(conn):
        now = iso_now()
        for value in inputs:
            _store_item(conn, value, classifications[value["message_id"]], now=now)
    return len(inputs)


def scan_mail_catalog(
    conn: sqlite3.Connection,
    config: Config,
    *,
    runner: Callable[..., CommandResult] = run_mail_json,
    classifier: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
    max_pages: int = 1,
    page_size: int = 100,
    restart: bool = False,
    resume_failed: bool = False,
) -> dict[str, Any]:
    """Resume a bounded all-mail scan without persisting message bodies."""
    if not config.feature("mail"):
        raise MailCatalogError("mail feature is disabled")
    if classifier is None:
        raise MailCatalogError("AI mail classifier is unavailable")
    if not 1 <= max_pages <= 10 or not 1 <= page_size <= 100:
        raise MailCatalogError("mail catalog bounds are invalid")
    run = _run_row(conn, page_size=page_size, restart=restart, resume_failed=resume_failed)
    if run['state'] == 'failed':
        return {'run_id': run['run_id'], 'state': 'failed', 'incomplete': True,
                'pages_this_call': 0, 'pages_processed': run['pages_processed'],
                'reason': json.loads(run['error_json'] or '{}').get('reason', run['last_error']),
                'resume_required': True}
    review_reclassified = _reclassify_unresolved(conn)
    prune_cursor_history(conn)
    run_id = str(run["run_id"])
    folders = json.loads(run["folders_json"])
    run_page_size = int(run["page_size"])
    if run["state"] == "complete":
        indexed = _index_pending_inbound(conn, classifier, limit=page_size)
        return {
            "run_id": run_id,
            "state": "complete",
            "pages_this_call": 0,
            "pages_processed": run["pages_processed"],
            "messages_seen": run["messages_seen"],
            "folder_index": run["folder_index"],
            "has_cursor": False,
            "incremental_indexed": indexed,
            "page_size": run_page_size,
            "review_reclassified": review_reclassified,
        }
    processed = 0
    try:
        while processed < max_pages and int(run["folder_index"]) < len(folders):
            if run['pages_processed'] >= run['page_limit']:
                raise MailCatalogError('mail catalog run page budget exhausted')
            folder = str(folders[int(run["folder_index"])])
            argv = [
                "mail",
                "+triage",
                "--folder-id",
                folder,
                "--max",
                str(run_page_size),
                "--format",
                "json",
                "--as",
                "user",
            ]
            if run["next_page_token"]:
                argv.extend(("--page-token", str(run["next_page_token"])))
            listed_result = runner(argv)
            page = decode_page(_data(listed_result), meta=listed_result.meta,
                               current_token=run['next_page_token'], allow_empty_more=True)
            summaries = page.messages
            message_ids = [item['message_id'] for item in summaries]
            if page.has_more and conn.execute('SELECT 1 FROM mail_catalog_cursors WHERE run_id=? AND folder_index=? AND token_digest=?',
                (run_id, run['folder_index'], digest(page.next_token))).fetchone():
                raise PaginationError('mail catalog pagination token cycle')
            detail_by_id: dict[str, dict[str, Any]] = {}
            if message_ids:
                details = _data(
                    runner(
                        [
                            "mail",
                            "+messages",
                            "--message-ids",
                            ",".join(message_ids),
                            "--html=false",
                            "--format",
                            "json",
                            "--as",
                            "user",
                        ]
                    )
                )
                detail_by_id = {
                    str(item.get("message_id")): item
                    for item in details.get("messages") or []
                    if isinstance(item, dict) and item.get("message_id")
                }
                if set(detail_by_id) != set(message_ids) or details.get("unavailable_message_ids"):
                    raise MailCatalogError("mail catalog detail batch is incomplete")
            inputs = [
                _classification_input(summary, detail_by_id[str(summary["message_id"])])
                for summary in summaries
            ]
            classifications = _classify_batches(inputs, classifier)
            has_more = page.has_more
            next_index = int(run["folder_index"]) if has_more else int(run["folder_index"]) + 1
            next_token = page.next_token
            complete = next_index >= len(folders)
            now = iso_now()
            with transaction(conn):
                current = conn.execute('SELECT revision,state FROM mail_catalog_runs WHERE run_id=?', (run_id,)).fetchone()
                if current is None or tuple(current) != (run['revision'], 'running'):
                    raise MailCatalogStale('mail catalog checkpoint changed concurrently')
                if has_more and conn.execute('SELECT 1 FROM mail_catalog_cursors WHERE run_id=? AND folder_index=? AND token_digest=?',
                    (run_id, run['folder_index'], digest(next_token))).fetchone():
                    raise PaginationError('mail catalog pagination token cycle')
                conn.execute('INSERT OR IGNORE INTO mail_catalog_cursors VALUES(?,?,?,?)',
                             (run_id, run['folder_index'], digest(run['next_page_token']), now))
                for value in inputs:
                    item = classifications[value["message_id"]]
                    _store_item(conn, value, item, now=now)
                updated = conn.execute(
                    """UPDATE mail_catalog_runs SET folder_index=?,next_page_token=?,
                           pages_processed=pages_processed+1,
                           messages_seen=messages_seen+?,state=?,last_error=NULL,error_json=NULL,updated_at=?,revision=revision+1
                         WHERE run_id=? AND revision=? AND state='running' AND folder_index=? AND next_page_token IS ? AND pages_processed=?""",
                    (
                        next_index,
                        next_token,
                        len(inputs),
                        "complete" if complete else "running",
                        now,
                        run_id,
                        run['revision'], run['folder_index'], run['next_page_token'], run['pages_processed'],
                    ),
                ).rowcount
                if updated != 1:
                    raise MailCatalogStale('mail catalog checkpoint CAS failed')
            processed += 1
            run = conn.execute(
                "SELECT * FROM mail_catalog_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return {
            "run_id": run_id,
            "state": run["state"],
            "pages_this_call": processed,
            "pages_processed": run["pages_processed"],
            "messages_seen": run["messages_seen"],
            "folder_index": run["folder_index"],
            "has_cursor": bool(run["next_page_token"]),
            "page_size": run_page_size,
            "review_reclassified": review_reclassified,
        }
    except Exception as exc:
        with transaction(conn):
            conn.execute(
                "UPDATE mail_catalog_runs SET state='failed',last_error=?,error_json=?,updated_at=?,revision=revision+1 WHERE run_id=? AND revision=? AND state='running'",
                (type(exc).__name__, canonical_json({'reason': 'pagination_protocol_error' if isinstance(exc, PaginationError)
                    else 'page_budget_exhausted' if run['pages_processed'] >= run['page_limit'] else 'classification_or_transport_error',
                    'error_class': type(exc).__name__, 'incomplete': True}), iso_now(), run_id, run['revision']),
            )
        if isinstance(exc, PaginationError):
            raise MailCatalogError(str(exc)) from exc
        raise


def catalog_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    latest = conn.execute(
        "SELECT * FROM mail_catalog_runs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    categories = {
        str(row["category"]): int(row["count"])
        for row in conn.execute(
            "SELECT category,count(*) AS count FROM mail_catalog_items GROUP BY category ORDER BY count DESC"
        )
    }
    attention = {
        str(row["attention"]): int(row["count"])
        for row in conn.execute(
            "SELECT attention,count(*) AS count FROM mail_catalog_items GROUP BY attention ORDER BY count DESC"
        )
    }
    needs_review = conn.execute(
        "SELECT count(*) FROM mail_catalog_items WHERE classification_source='ai_unresolved_v1'"
    ).fetchone()[0]
    return {
        "run": dict(latest) if latest is not None else None,
        "indexed_messages": sum(categories.values()),
        "categories": categories,
        "attention": attention,
        "needs_review": int(needs_review),
    }


def query_catalog(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    attention: str | None = None,
    topic: str | None = None,
    needs_review: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if category is not None and category not in CATEGORIES:
        raise MailCatalogError("unknown mail category")
    if attention is not None and attention not in ATTENTION:
        raise MailCatalogError("unknown attention state")
    if topic is not None and topic not in TOPICS:
        raise MailCatalogError("unknown mail topic")
    clauses = []
    values: list[Any] = []
    if category:
        clauses.append("category=?")
        values.append(category)
    if attention:
        clauses.append("attention=?")
        values.append(attention)
    if topic:
        clauses.append("EXISTS (SELECT 1 FROM json_each(topics_json) WHERE value=?)")
        values.append(topic)
    if needs_review:
        clauses.append("classification_source='ai_unresolved_v1'")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    values.append(max(1, min(limit, 200)))
    return [
        {**dict(row), "labels": json.loads(row["labels_json"]), "topics": json.loads(row["topics_json"])}
        for row in conn.execute(
            f"""SELECT message_id,thread_id,folder,sender_name,sender_address,subject,
                       internal_date,labels_json,category,origin,attention,topics_json,
                       confidence,classification_source
                  FROM mail_catalog_items{where}
                 ORDER BY internal_date DESC LIMIT ?""",
            values,
        )
    ]
