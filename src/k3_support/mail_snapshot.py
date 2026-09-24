"""Historical mail membership is evidence, not a live re-run of classification.

No mailbox mutation, body read or outbound transport lives in this module.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any

from .db import transaction
from .ids import canonical_json, digest, new_id
from .mail_catalog import ATTENTION, CATEGORIES, _deterministic_classification
from .timeutil import iso_now, parse_iso

CATEGORY_LABELS = {
    "build_ci": "构建与 CI", "code_review": "代码评审", "upstream": "Upstream",
    "company": "公司事务", "project_release": "项目与发布", "support_bug": "支持与 Bug",
    "meeting": "会议与日程", "security_account": "安全与账号",
    "external": "外部联系", "other": "其他",
}

# Shared by the body backfill and summary reader. A delivered timestamp alone
# cannot exclude a message discovered after that time's summary was frozen.
UNASSIGNED_MAIL_SQL = """NOT EXISTS(
    SELECT 1 FROM mail_summary_membership sm WHERE sm.message_id=mi.message_id)
    AND NOT EXISTS(SELECT 1 FROM mail_summary_legacy_exclusions le WHERE le.message_id=mi.message_id)"""


class MailSnapshotError(ValueError):
    pass


def capture_members(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Copy exact catalog decisions before invoking the summarizer.

    Unknown classifications remain visibly unknown. Do not infer per-message
    membership from an LLM's aggregate counts or recategorize the old digest.
    Caller holds one read snapshot covering both mail selection and this call.
    """
    members = []
    for row in rows:
        catalog = conn.execute(
            "SELECT * FROM mail_catalog_items WHERE message_id=?", (row["message_id"],)
        ).fetchone()
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        value = {
            "message_id": str(row["message_id"]), "subject": row["subject"],
            "sender_name": row["sender_name"], "sender_address": row["sender_address"],
            "body_excerpt": str(row["body_preview"] or "")[:800],
            "labels": payload.get("label_ids") or [],
        }
        fallback = _deterministic_classification(value) if catalog is None else None
        classification = dict(catalog) if catalog else (fallback or {})
        category = classification.get("category", "other")
        attention = classification.get("attention", "information")
        if row["classification"] == "urgent_action":
            attention = "blocked"
        elif row["classification"] == "action" and attention == "information":
            attention = "action_required"
        if category not in CATEGORIES or attention not in ATTENTION:
            raise MailSnapshotError("invalid catalog classification")
        thread_id = classification.get("thread_id") or payload.get("thread_id") or row["thread_id"]
        members.append({
            "message_id": value["message_id"], "category": category, "attention": attention,
            "thread_id": str(thread_id) if thread_id else None,
            "received_epoch_ms": int(row["internal_date"] or 0),
            "subject": row["subject"] or "（无主题）",
            "sender": row["sender_name"] or row["sender_address"] or "未知发件人",
            "classification_source": classification.get("classification_source") or
                                     classification.get("_source") or "unclassified",
            "needs_classification_review": not bool(classification) or
                                           str(classification.get("classification_source", "")).startswith("ai_unresolved"),
        })
    return members


def freeze_members(conn: sqlite3.Connection, digest_id: str, members: list[dict[str, Any]]) -> str:
    """Called in the same transaction as digest creation, once only."""
    snapshot_digest = digest(members)
    for ordinal, member in enumerate(members):
        conn.execute(
            """INSERT INTO mail_summary_membership(digest_id,message_id,ordinal,category,
                   attention,thread_id,received_epoch_ms,metadata_json) VALUES(?,?,?,?,?,?,?,?)""",
            (digest_id, member["message_id"], ordinal, member["category"], member["attention"],
             member["thread_id"], member["received_epoch_ms"], canonical_json(member)),
        )
    conn.execute("UPDATE mail_digest_runs SET membership_digest=? WHERE digest_id=?", (snapshot_digest, digest_id))
    return snapshot_digest


def bind_category_counts(summary: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    """The model explains categories; the frozen membership owns exact counts."""
    counts = Counter(member["category"] for member in members)
    explanations = {item["category"]: item["summary"] for item in summary["categories"]}
    return {**summary, "categories": [
        {"category": category, "count": count,
         "summary": explanations.get(category, "按已保存分类归集，可展开查看。")}
        for category, count in sorted(counts.items())
    ]}


def classification_review_digest(conn: sqlite3.Connection, message_id: str) -> str:
    """Exact review version, including correction history to prevent an ABA."""
    item = conn.execute("SELECT category,updated_at FROM mail_catalog_items WHERE message_id=?", (message_id,)).fetchone()
    if item is None:
        raise MailSnapshotError("mail catalog message not found")
    correction = conn.execute(
        "SELECT correction_id FROM mail_category_corrections WHERE message_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (message_id,),
    ).fetchone()
    return digest({"message_id": message_id, "category": item["category"], "updated_at": item["updated_at"],
                   "latest_correction_id": correction[0] if correction else None})


def correct_category(
    conn: sqlite3.Connection, *, message_id: str, category: str, actor_id: str,
    reason: str, expected_updated_at: str, external_id: str,
    expected_current_digest: str | None = None,
) -> dict[str, Any]:
    """Apply an explicit operator correction; caller authenticates the operator."""
    if category not in CATEGORIES or not actor_id.strip() or not reason.strip() or not external_id:
        raise MailSnapshotError("category, operator, reason and external ID are required")
    request_digest = digest({"message_id": message_id, "category": category, "actor_id": actor_id,
                             "reason": reason, "expected_updated_at": expected_updated_at,
                             "expected_current_digest": expected_current_digest})
    with transaction(conn):
        previous = conn.execute("SELECT * FROM mail_category_corrections WHERE external_id=?", (external_id,)).fetchone()
        if previous:
            if previous["request_digest"] != request_digest:
                raise MailSnapshotError("correction replay does not match original request")
            return {**dict(previous), "applied": False}
        item = conn.execute("SELECT * FROM mail_catalog_items WHERE message_id=?", (message_id,)).fetchone()
        if item is None:
            raise MailSnapshotError("mail catalog message not found")
        if item["updated_at"] != expected_updated_at:
            raise MailSnapshotError("mail classification changed; refresh before correcting")
        if expected_current_digest is not None and classification_review_digest(conn, message_id) != expected_current_digest:
            raise MailSnapshotError("mail classification review changed; refresh before correcting")
        correction_id, now = new_id("mcc"), iso_now()
        conn.execute(
            """INSERT INTO mail_category_corrections VALUES(?,?,?,?,?,?,?,?,?)""",
            (correction_id, message_id, category, item["category"], actor_id, reason, now, external_id, request_digest),
        )
        conn.execute(
            """UPDATE mail_catalog_items SET category=?,classification_source='operator',updated_at=? WHERE message_id=?""",
            (category, now, message_id),
        )
        return {"correction_id": correction_id, "message_id": message_id, "category": category, "applied": True}


def query_summary(
    conn: sqlite3.Connection, *, digest_id: str, category: str | None = None,
    attention: str | None = None, page: int = 1, page_size: int = 20,
    since: str | None = None, until: str | None = None,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    if category is not None and category not in CATEGORIES:
        raise MailSnapshotError("unknown mail category")
    if attention is not None and attention not in ATTENTION:
        raise MailSnapshotError("unknown mail attention state")
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100:
        raise MailSnapshotError("invalid pagination")
    bounds = []
    for value in (since, until):
        parsed = parse_iso(value) if value else None
        if parsed is not None and parsed.tzinfo is None:
            raise MailSnapshotError("date bounds must include timezone")
        bounds.append(int(parsed.timestamp() * 1000) if parsed else None)
    if all(value is not None for value in bounds) and bounds[0] >= bounds[1]:
        raise MailSnapshotError("since must precede until")
    # A SAVEPOINT gives a read snapshot without the write lock of transaction().
    savepoint = new_id("mail_read")
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        run = conn.execute("SELECT * FROM mail_digest_runs WHERE digest_id=?", (digest_id,)).fetchone()
        if run is None:
            raise MailSnapshotError("mail summary not found")
        if run["membership_digest"] is None:
            return {"digest_id": digest_id, "state": "legacy_membership_unavailable", "historical_count": run["item_count"],
                    "items": [], "read_only": True, "reason": "旧摘要未保存逐封成员，不能据今天的邮箱重建历史分类。"}
        if expected_digest is not None and expected_digest != run["membership_digest"]:
            raise MailSnapshotError("summary snapshot changed; reopen summary")
        where = "m.digest_id=?"
        params: list[Any] = [digest_id]
        for column, value in (("category", category), ("attention", attention)):
            if value is not None:
                where += f" AND m.{column}=?"
                params.append(value)
        for operator, bound in ((">=", bounds[0]), ("<", bounds[1])):
            if bound is not None:
                where += f" AND m.received_epoch_ms {operator} ?"
                params.append(bound)
        counts = dict(conn.execute(
            """SELECT count(*) message_count,count(DISTINCT coalesce(thread_id,'message:'||message_id)) thread_count
                 FROM mail_summary_membership m WHERE """ + where, params,
        ).fetchone())
        page_count = max(1, (counts["message_count"] + page_size - 1) // page_size)
        if page > page_count:
            raise MailSnapshotError("page is outside this summary")
        rows = conn.execute(
            """SELECT m.metadata_json,mc.category current_category,l.message_app_link
                 FROM mail_summary_membership m LEFT JOIN mail_catalog_items mc USING(message_id)
                 LEFT JOIN mail_digest_links l ON l.digest_id=m.digest_id AND l.message_id=m.message_id
                 WHERE """ + where + " ORDER BY m.ordinal LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        categories = [dict(row) for row in conn.execute(
            "SELECT category,count(*) count FROM mail_summary_membership WHERE digest_id=? GROUP BY category ORDER BY category", (digest_id,)
        )]
        items = []
        for row in rows:
            item = json.loads(row["metadata_json"])
            item.update(current_category=row["current_category"] or item["category"], message_app_link=row["message_app_link"])
            item["classification_changed_since_summary"] = item["current_category"] != item["category"]
            items.append(item)
        return {"digest_id": digest_id, "snapshot_digest": run["membership_digest"], "state": run["state"],
                "range_start": run["range_start"], "range_end": run["range_end"], "timezone": run["timezone"],
                "historical_count": run["item_count"], "categories": categories, **counts,
                "page": page, "page_count": page_count, "items": items, "read_only": True}
    finally:
        conn.execute(f"RELEASE {savepoint}")
