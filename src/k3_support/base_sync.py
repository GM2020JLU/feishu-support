from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config
from .db import transaction
from .ids import canonical_json, digest, new_id
from .lark import CommandResult, run_json
from .timeutil import iso_now
from .base_sync_attempt import AttemptRef
from .base_sync_protocol import (
    BaseSyncError, BaseSyncBusy, BaseSyncInputChanged, BaseSyncSuperseded, synchronize, resume_unblocked, job_input_digest,
)


CASE_TYPES = ("faq", "investigation", "bug", "incident", "request", "mail", "meeting")
SEVERITIES = ("P0", "P1", "P2", "P3")
DISCLOSURE_CLASSES = ("public", "internal", "team", "private", "restricted")
KNOWLEDGE_STATES = ("candidate", "approved", "stale", "retired")
MAIL_CLASSES = ("urgent_action", "action", "waiting", "information", "noise")


BASE_TABLE_BLUEPRINTS: dict[str, list[dict[str, Any]]] = {
    "Cases": [
        {"name": "Case ID", "type": "text"},
        {"name": "标题", "type": "text"},
        {
            "name": "类型",
            "type": "select",
            "multiple": False,
            "options": [{"name": value} for value in CASE_TYPES],
        },
        {
            "name": "严重度",
            "type": "select",
            "multiple": False,
            "options": [{"name": value} for value in SEVERITIES],
        },
        {"name": "状态", "type": "text"},
        {"name": "置信度", "type": "number"},
        {"name": "提问人", "type": "text"},
        {"name": "来源会话", "type": "text"},
        {
            "name": "披露级别",
            "type": "select",
            "multiple": False,
            "options": [{"name": value} for value in DISCLOSURE_CLASSES],
        },
        {"name": "负责人", "type": "text"},
        {"name": "下一动作", "type": "text"},
        {"name": "当前 Job", "type": "text"},
        {"name": "board 会话", "type": "text"},
        {"name": "证据层级", "type": "text"},
        {"name": "canonical Case", "type": "text"},
        {
            "name": "更新时间",
            "type": "datetime",
            "style": {"format": "yyyy-MM-dd HH:mm"},
        },
        {"name": "版本", "type": "number"},
    ],
    "Knowledge Review": [
        {"name": "Knowledge ID", "type": "text"},
        {"name": "标题", "type": "text"},
        {
            "name": "状态",
            "type": "select",
            "multiple": False,
            "options": [{"name": value} for value in KNOWLEDGE_STATES],
        },
        {"name": "问题变体", "type": "text"},
        {"name": "候选答案", "type": "text"},
        {"name": "项目", "type": "text"},
        {"name": "模块", "type": "text"},
        {"name": "硬件", "type": "text"},
        {"name": "软件版本", "type": "text"},
        {"name": "适用范围", "type": "text"},
        {
            "name": "披露级别",
            "type": "select",
            "multiple": False,
            "options": [{"name": value} for value in DISCLOSURE_CLASSES],
        },
        {"name": "来源", "type": "text"},
        {"name": "证据层级", "type": "text"},
        {"name": "置信度", "type": "number"},
        {"name": "审核人", "type": "text"},
        {
            "name": "审核时间",
            "type": "datetime",
            "style": {"format": "yyyy-MM-dd HH:mm"},
        },
        {
            "name": "复审期限",
            "type": "datetime",
            "style": {"format": "yyyy-MM-dd HH:mm"},
        },
        {"name": "使用次数", "type": "number"},
        {"name": "成功率", "type": "number"},
        {"name": "纠正次数", "type": "number"},
        {"name": "canonical Case", "type": "text"},
        {
            "name": "更新时间",
            "type": "datetime",
            "style": {"format": "yyyy-MM-dd HH:mm"},
        },
    ],
    "Mail Digest": [
        {"name": "邮件 ID", "type": "text"},
        {"name": "发件人", "type": "text"},
        {"name": "发件地址", "type": "text", "style": {"type": "email"}},
        {"name": "主题", "type": "text"},
        {
            "name": "分类",
            "type": "select",
            "multiple": False,
            "options": [{"name": value} for value in MAIL_CLASSES],
        },
        {"name": "重要原因", "type": "text"},
        {"name": "请求动作", "type": "text"},
        {
            "name": "截止时间",
            "type": "datetime",
            "style": {"format": "yyyy-MM-dd HH:mm"},
        },
        {"name": "已通知", "type": "checkbox"},
        {"name": "关联 Case", "type": "text"},
        {
            "name": "收件时间",
            "type": "datetime",
            "style": {"format": "yyyy-MM-dd HH:mm"},
        },
        {
            "name": "更新时间",
            "type": "datetime",
            "style": {"format": "yyyy-MM-dd HH:mm"},
        },
    ],
    "System Health": [
        {"name": "组件", "type": "text"},
        {"name": "状态", "type": "text"},
        {
            "name": "最后 heartbeat",
            "type": "datetime",
            "style": {"format": "yyyy-MM-dd HH:mm"},
        },
        {"name": "队列深度", "type": "number"},
        {"name": "延迟秒", "type": "number"},
        {"name": "错误分类", "type": "text"},
        {"name": "安全摘要", "type": "text"},
    ],
}


ENTITY_TABLE_KEYS = {
    "case": "cases_table_id",
    "knowledge": "knowledge_table_id",
    "mail": "mail_table_id",
    "health": "health_table_id",
}

ENTITY_PRIMARY_FIELDS = {
    "case": "Case ID",
    "knowledge": "Knowledge ID",
    "mail": "邮件 ID",
    "health": "组件",
}


def _json_list(value: str) -> list[Any]:
    parsed = json.loads(value)
    return parsed if isinstance(parsed, list) else []


def _trim(value: Any, limit: int = 4000) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _remote_exact_record_ids(
    data: Any, *, primary_field: str, entity_id: str
) -> list[str]:
    if not isinstance(data, dict):
        return []
    candidates = data.get("records") or data.get("items") or data.get("data") or []
    if not isinstance(candidates, list):
        return []
    matrix_fields = data.get("fields")
    matrix_record_ids = data.get("record_id_list")
    matches: list[str] = []
    for index, item in enumerate(candidates):
        if (
            isinstance(item, list)
            and isinstance(matrix_fields, list)
            and isinstance(matrix_record_ids, list)
            and index < len(matrix_record_ids)
        ):
            item = {
                "record_id": matrix_record_ids[index],
                "fields": dict(zip(matrix_fields, item, strict=False)),
            }
        if not isinstance(item, dict):
            continue
        record_id = item.get("record_id") or item.get("id")
        field_value = item.get("fields")
        fields: dict[str, Any] = field_value if isinstance(field_value, dict) else item
        value = fields.get(primary_field)
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if isinstance(value, dict):
            value = value.get("text") or value.get("value")
        if str(value or "") == entity_id and isinstance(record_id, str):
            matches.append(record_id)
    return matches


def case_fields(conn: sqlite3.Connection, case: sqlite3.Row) -> dict[str, Any]:
    layers = [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT evidence_layer FROM evidence WHERE case_id=? "
            "ORDER BY evidence_layer",
            (case["case_id"],),
        )
    ]
    return {
        "Case ID": case["case_id"],
        "标题": _trim(case["title"]),
        "类型": [case["type"]],
        "严重度": [case["severity"]],
        "状态": case["state"],
        "置信度": float(case["confidence"]),
        "提问人": case["requester_id"] or "",
        "来源会话": case["requester_chat_id"] or "",
        "披露级别": [case["disclosure_class"]],
        "负责人": case["owner"],
        "下一动作": _trim(case["next_action"]),
        "当前 Job": case["active_job_id"] or "",
        "board 会话": case["active_session_id"] or "",
        "证据层级": ", ".join(layers),
        "canonical Case": case["canonical_case_id"] or "",
        "更新时间": case["updated_at"],
        "版本": int(case["version"]),
    }


def knowledge_fields(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    sources = conn.execute(
        """SELECT url,source_type,stable_external_id FROM knowledge_sources
           WHERE knowledge_id=? ORDER BY mapping_id""",
        (row["knowledge_id"],),
    ).fetchall()
    source_lines = []
    for item in sources:
        url = str(item["url"] or "")
        if url.startswith(("https://", "http://")):
            source_lines.append(url)
        else:
            source_lines.append(f"{item['source_type']} (internal evidence)")
    use_count = int(row["use_count"])
    return {
        "Knowledge ID": row["knowledge_id"],
        "标题": _trim(row["title"]),
        "状态": [row["status"]],
        "问题变体": _trim(
            "\n".join(str(item) for item in _json_list(row["question_variants_json"]))
        ),
        "候选答案": _trim(row["answer_markdown"]),
        "项目": row["project"] or "",
        "模块": row["module"] or "",
        "硬件": row["hardware"] or "",
        "软件版本": row["software_version"] or "",
        "适用范围": _trim(row["applicability"]),
        "披露级别": [row["disclosure_class"]],
        "来源": _trim("\n".join(source_lines)),
        "证据层级": ", ".join(
            str(item) for item in _json_list(row["evidence_layers_json"])
        ),
        "置信度": float(row["confidence"]),
        "审核人": row["reviewed_by"] or "",
        "审核时间": row["reviewed_at"],
        "复审期限": row["review_due_at"],
        "使用次数": use_count,
        "成功率": float(row["success_count"]) / use_count if use_count else 0.0,
        "纠正次数": int(row["correction_count"]),
        "canonical Case": row["canonical_case_id"] or "",
        "更新时间": row["updated_at"],
    }


def mail_fields(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "邮件 ID": row["message_id"],
        "发件人": row["sender_name"] or "",
        "发件地址": row["sender_address"] or None,
        "主题": _trim(row["subject"]),
        "分类": [row["classification"]] if row["classification"] else [],
        "重要原因": _trim(row["classification_reason"]),
        "请求动作": _trim(row["requested_action"]),
        "截止时间": row["deadline"],
        "已通知": bool(row["notified"]),
        "关联 Case": row["case_id"] or "",
        "收件时间": row["received_at"],
        "更新时间": row["updated_at"],
    }


HEALTH_DETAIL_ALLOWLIST = {
    "mode",
    "ready",
    "inbox_depth",
    "job_depth",
    "outbox_depth",
    "delivery_enabled",
    "execution_enabled",
    "claimed",
    "processed",
    "delivered",
}


def health_fields(row: sqlite3.Row) -> dict[str, Any]:
    raw = json.loads(row["detail_json"])
    detail = {
        key: raw[key]
        for key in sorted(HEALTH_DETAIL_ALLOWLIST)
        if key in raw and isinstance(raw[key], (str, int, float, bool, type(None)))
    }
    depth = next(
        (
            float(raw[key])
            for key in ("inbox_depth", "job_depth", "outbox_depth")
            if isinstance(raw.get(key), (int, float))
        ),
        None,
    )
    lag = next(
        (
            float(raw[key])
            for key in ("lag_seconds", "oldest_lag_seconds")
            if isinstance(raw.get(key), (int, float))
        ),
        None,
    )
    error = raw.get("error")
    error_class = ""
    if isinstance(error, str):
        candidate = error.split(":", 1)[0]
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", candidate):
            error_class = candidate
    return {
        "组件": row["component"],
        "状态": row["status"],
        "最后 heartbeat": row["heartbeat_at"],
        "队列深度": depth,
        "延迟秒": lag,
        "错误分类": error_class,
        "安全摘要": canonical_json(detail),
    }


def _entity_payload(
    conn: sqlite3.Connection, entity_type: str, entity_id: str
) -> tuple[dict[str, Any], int]:
    if entity_type == "case":
        row = conn.execute(
            "SELECT * FROM cases WHERE case_id=?", (entity_id,)
        ).fetchone()
        if row is None:
            raise BaseSyncError("case not found")
        return case_fields(conn, row), int(row["version"])
    if entity_type == "knowledge":
        row = conn.execute(
            "SELECT * FROM knowledge_entries WHERE knowledge_id=?", (entity_id,)
        ).fetchone()
        if row is None:
            raise BaseSyncError("knowledge entry not found")
        return knowledge_fields(conn, row), 1
    if entity_type == "mail":
        row = conn.execute(
            "SELECT * FROM mail_items WHERE message_id=?", (entity_id,)
        ).fetchone()
        if row is None:
            raise BaseSyncError("mail item not found")
        return mail_fields(row), 1
    if entity_type == "health":
        row = conn.execute(
            "SELECT * FROM service_state WHERE component=?", (entity_id,)
        ).fetchone()
        if row is None:
            raise BaseSyncError("health component not found")
        return health_fields(row), 1
    raise BaseSyncError(f"unsupported Base entity type: {entity_type}")


def sync_entity(
    conn: sqlite3.Connection,
    config: Config,
    *,
    entity_type: str,
    entity_id: str,
    runner: Callable[..., CommandResult] = run_json,
) -> dict[str, Any]:
    """Direct callers receive a durable operation ID; no worker identity is inferred."""
    return synchronize(conn, config, entity_type=entity_type, entity_id=entity_id, runner=runner)


def sync_case(
    conn: sqlite3.Connection,
    config: Config,
    *,
    case_id: str,
    runner: Callable[..., CommandResult] = run_json,
) -> dict[str, Any]:
    result = sync_entity(
        conn, config, entity_type="case", entity_id=case_id, runner=runner
    )
    return {
        "case_id": case_id,
        "record_id": result["record_id"],
        "version": result["version"],
    }


def _entity_ids(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    values.extend(
        ("case", str(row[0])) for row in conn.execute("SELECT case_id FROM cases")
    )
    values.extend(
        ("knowledge", str(row[0]))
        for row in conn.execute("SELECT knowledge_id FROM knowledge_entries")
    )
    values.extend(
        ("mail", str(row[0]))
        for row in conn.execute("SELECT message_id FROM mail_items")
    )
    values.extend(
        ("health", str(row[0]))
        for row in conn.execute("SELECT component FROM service_state")
    )
    return values


def enqueue_dirty_entities(
    conn: sqlite3.Connection, config: Config, *, limit: int = 500
) -> dict[str, Any]:
    if config.mode != "active" or not config.feature("base_sync"):
        return {"enabled": False, "queued": 0, "unchanged": 0, "exhausted": []}
    from .runtime_control import capability_allowed

    if not capability_allowed(conn, config, "base_sync"):
        return {"enabled": False, "queued": 0, "unchanged": 0, "exhausted": []}
    queued = 0
    unchanged = 0
    exhausted: list[dict[str, str]] = []
    now = iso_now()
    with transaction(conn):
        resume_unblocked(conn, config)
        for entity_type, entity_id in _entity_ids(conn):
            fields, _version = _entity_payload(conn, entity_type, entity_id)
            mirror_digest = digest(fields)
            mapping = conn.execute(
                "SELECT mirrored_digest FROM base_mappings "
                "WHERE entity_type=? AND entity_id=? AND table_id=? AND base_digest=?",
                (entity_type, entity_id, str(config.raw['base'][ENTITY_TABLE_KEYS[entity_type]]), digest(config.raw['base']['app_token'])),
            ).fetchone()
            if mapping is not None and mapping["mirrored_digest"] == mirror_digest:
                unchanged += 1
                continue
            target_base = digest(config.raw['base']['app_token'])
            target_table = str(config.raw['base'][ENTITY_TABLE_KEYS[entity_type]])
            context = canonical_json({"entity_id": entity_id, "entity_type": entity_type,
                                      "base_digest": target_base, "table_id": target_table})
            input_digest = job_input_digest(entity_id=entity_id, entity_type=entity_type,
                                           mirror=mirror_digest, base_digest=target_base, table_id=target_table)
            existing = conn.execute(
                """SELECT job_id,state,attempt_no,max_attempts FROM jobs
                   WHERE job_type='base_sync' AND input_digest=? AND context_json=?
                   ORDER BY created_at DESC LIMIT 1""",
                (input_digest, context),
            ).fetchone()
            if existing is not None:
                if existing["state"] in {"queued", "running", "waiting"}:
                    continue
                if existing["state"] in {"failed", "orphaned"} and int(
                    existing["attempt_no"]
                ) < int(existing["max_attempts"]):
                    conn.execute(
                        """UPDATE jobs SET state='queued',available_at=?,lease_owner=NULL,
                           lease_expires_at=NULL,error_class=NULL,updated_at=?
                           WHERE job_id=?""",
                        (now, now, existing["job_id"]),
                    )
                    queued += 1
                    continue
                exhausted.append({"entity_type": entity_type, "entity_id": entity_id})
                continue
            case_id = entity_id if entity_type == "case" else None
            conn.execute(
                """INSERT INTO jobs(job_id,case_id,job_type,state,priority,input_digest,
                       max_attempts,available_at,created_at,updated_at,context_json)
                   VALUES(?,?,'base_sync','queued',200,?,8,?,?,?,?)""",
                (
                    new_id("job"),
                    case_id,
                    input_digest,
                    now,
                    now,
                    now,
                    context,
                ),
            )
            queued += 1
            if queued >= limit:
                break
    return {
        "enabled": True,
        "queued": queued,
        "unchanged": unchanged,
        "exhausted": exhausted,
    }


def run_base_sync_job(
    conn: sqlite3.Connection,
    config: Config,
    *,
    job_id: str,
    attempt_ref: AttemptRef,
    runner: Callable[..., CommandResult] = run_json,
) -> dict[str, Any]:
    if type(attempt_ref) is not AttemptRef or attempt_ref.job_id != job_id:
        raise BaseSyncError("original claimed Base attempt required")
    with transaction(conn):
        job = attempt_ref.current(conn)
        if job is None:
            raise BaseSyncSuperseded("Base attempt is no longer owned")
        context = json.loads(job["context_json"])
        entity_type, entity_id = context.get("entity_type"), context.get("entity_id")
        if entity_type not in ENTITY_TABLE_KEYS or not isinstance(entity_id, str):
            raise BaseSyncError("Base sync job context is invalid")
        conn.execute("""INSERT OR IGNORE INTO job_attempts(
            attempt_id,job_id,attempt_no,started_at,worker_id) VALUES(?,?,?,?,?)""",
            (new_id("jat"), job_id, attempt_ref.attempt_no, iso_now(), attempt_ref.owner))
    return synchronize(conn, config, entity_type=entity_type, entity_id=entity_id,
                       runner=runner, attempt_ref=attempt_ref)


def fail_base_sync_job(
    conn: sqlite3.Connection, *, job_id: str, attempt_ref: AttemptRef, error: Exception
) -> dict[str, Any]:
    if type(attempt_ref) is not AttemptRef or attempt_ref.job_id != job_id:
        raise BaseSyncError("original claimed Base attempt required")
    now = iso_now()
    with transaction(conn):
        job = attempt_ref.current(conn, now=now)
        if job is None:
            return {"job_id": job_id, "state": "superseded", "retry": False, "error": type(error).__name__}
        uncertain = conn.execute("""SELECT 1 FROM base_sync_operations WHERE job_id=?
            AND attempt_no=? AND lease_owner=? AND state IN ('dispatched','unknown')""",
            (job_id, attempt_ref.attempt_no, attempt_ref.owner)).fetchone()
        held = uncertain is not None or isinstance(error, BaseSyncBusy)
        obsolete = isinstance(error, BaseSyncInputChanged)
        retry = not held and not obsolete and attempt_ref.attempt_no < int(job["max_attempts"])
        state = "waiting" if held else "cancelled" if obsolete else "queued" if retry else "failed"
        delay = min(1800, 15 * (3 ** max(0, attempt_ref.attempt_no - 1)))
        available = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat() if retry else now
        detail = {"error": type(error).__name__, "retry": retry, "remote_unknown": bool(uncertain)}
        updated = conn.execute("""UPDATE jobs SET state=?,available_at=?,error_class=?,lease_owner=NULL,
            lease_expires_at=NULL,updated_at=? WHERE job_id=? AND state='running'
            AND attempt_no=? AND lease_owner=? AND input_digest=? AND lifecycle_round=?""",
            (state, available, type(error).__name__, now, job_id, attempt_ref.attempt_no,
             attempt_ref.owner, attempt_ref.input_digest, attempt_ref.lifecycle_round)).rowcount
        if updated != 1:
            raise BaseSyncSuperseded("Base failure CAS failed")
        conn.execute("""UPDATE job_attempts SET ended_at=?,result='failed',detail_json=?
            WHERE job_id=? AND attempt_no=? AND worker_id=? AND ended_at IS NULL""",
            (now, canonical_json(detail), job_id, attempt_ref.attempt_no, attempt_ref.owner))
    return {"job_id": job_id, "state": state, "retry": retry, "error": detail["error"]}


def base_bootstrap_preview() -> dict[str, Any]:
    return {
        "name": "K3 AI 技术支持中心",
        "time_zone": "Asia/Shanghai",
        "first_table": "Cases",
        "fields": BASE_TABLE_BLUEPRINTS["Cases"],
        "additional_tables": {
            name: fields
            for name, fields in BASE_TABLE_BLUEPRINTS.items()
            if name != "Cases"
        },
        "authority": (
            "SQLite remains authoritative; Base is a mirror and review-input surface"
        ),
    }
