from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .db import transaction
from .ids import canonical_json, digest, new_id
from .lark import CommandResult, run_mail_json
from .mail_snapshot import (
    CATEGORY_LABELS,
    UNASSIGNED_MAIL_SQL,
    bind_category_counts,
    capture_members,
    freeze_members,
)
from .orchestrator import classify
from .store import enqueue_outbox
from .timeutil import iso_now, parse_iso


class MailError(ValueError):
    pass


MAIL_SUMMARY_SLOT_HOURS = {
    "mail_noon": 12,
    "mail_evening": 18,
}


def latest_summary_slot(
    slot: str,
    *,
    timezone: str,
    observed_at: datetime | None = None,
) -> str:
    """Return the most recent scheduled wall-clock instant for a digest slot.

    Persistent systemd timers may invoke a missed job after its nominal time.
    Anchoring the digest to the slot, instead of process start time, preserves
    the database idempotency key across restarts and scheduler migrations.
    """
    try:
        hour = MAIL_SUMMARY_SLOT_HOURS[slot]
    except KeyError as exc:
        raise MailError("invalid mail summary slot") from exc
    zone = ZoneInfo(timezone)
    observed = observed_at or datetime.now(zone)
    if observed.tzinfo is None:
        raise MailError("observed_at must include timezone")
    local = observed.astimezone(zone)
    scheduled = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if scheduled > local:
        scheduled -= timedelta(days=1)
    return scheduled.isoformat(timespec="seconds")


def _tx(conn: sqlite3.Connection):
    return nullcontext(conn) if conn.in_transaction else transaction(conn)


def _route_classification(route: dict[str, Any]) -> tuple[str, str | None]:
    route_name = str(route["route"])
    reasons = set(route.get("reason_codes") or [])
    if route_name == "urgent_notify":
        return "urgent_action", route_name
    if route_name == "ignore":
        return (
            ("noise", None)
            if "non_work_noise" in reasons
            else ("information", None)
        )
    if route_name in {"direct_answer", "clarify", "codex_debug", "owner_decision"}:
        return "action", route_name
    if route_name == "research" and float(route["confidence"]) >= 0.8:
        return "action", route_name
    return "information", None


def upsert_mail_item(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    case_id: str | None = None,
    route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message_id = payload.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise MailError("message_id is required")
    classification = classify(payload, "feishu_mail")
    mail_class = classification["mail_class"]
    requested_action = None
    classification_reason = (
        "keyword preclassification; requires evidence review for escalation"
    )
    confidence = float(classification["confidence"])
    if route is not None:
        mail_class, requested_action = _route_classification(route)
        classification_reason = "semantic route: " + str(route["route"])
        confidence = float(route["confidence"])
    sender = payload.get("head_from") or {}
    now = iso_now()
    with transaction(conn):
        conn.execute(
            """INSERT INTO mail_items(message_id,thread_id,sender_name,sender_address,subject,body_preview,
                   folder_id,label_ids_json,internal_date,classification,classification_reason,confidence,
                   requested_action,case_id,received_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(message_id) DO UPDATE SET thread_id=excluded.thread_id,subject=excluded.subject,
                   body_preview=excluded.body_preview,folder_id=excluded.folder_id,label_ids_json=excluded.label_ids_json,
                   classification=excluded.classification,classification_reason=excluded.classification_reason,
                   confidence=excluded.confidence,requested_action=excluded.requested_action,
                   case_id=coalesce(excluded.case_id,mail_items.case_id),updated_at=excluded.updated_at""",
            (
                message_id,
                payload.get("thread_id"),
                sender.get("name"),
                sender.get("mail_address"),
                payload.get("subject"),
                payload.get("body_preview"),
                payload.get("folder_id"),
                canonical_json(payload.get("label_ids") or []),
                payload.get("internal_date"),
                mail_class,
                classification_reason,
                confidence,
                requested_action,
                case_id,
                now,
                now,
            ),
        )
    return {
        "message_id": message_id,
        "classification": mail_class,
        "confidence": confidence,
    }


MAIL_SUMMARY_KEYS = {"overview", "categories", "important"}
MAIL_IMPORTANT_KEYS = {
    "message_id", "summary", "why_important", "action", "deadline"
}
MAIL_CATEGORY_KEYS = {"category", "count", "summary"}
MAIL_CATEGORIES = {
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


def _bounded_text(value: Any, label: str, *, limit: int, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MailError(f"AI mail summary {label} must be non-empty text")
    text = value.strip()
    if len(text) > limit:
        raise MailError(f"AI mail summary {label} is too long")
    return text


def validate_ai_summary(
    value: Any,
    *,
    allowed_message_ids: set[str],
    max_important: int,
    expected_count: int | None = None,
) -> dict[str, Any]:
    """Validate the model boundary and reject invented or duplicated mail IDs."""
    if not isinstance(value, dict) or set(value) != MAIL_SUMMARY_KEYS:
        raise MailError("AI mail summary has an invalid top-level schema")
    overview = _bounded_text(value["overview"], "overview", limit=1200)
    raw_categories = value["categories"]
    if not isinstance(raw_categories, list) or len(raw_categories) > len(MAIL_CATEGORIES):
        raise MailError("AI mail summary categories are invalid")
    categories = []
    seen_categories = set()
    for index, raw in enumerate(raw_categories):
        if not isinstance(raw, dict) or set(raw) != MAIL_CATEGORY_KEYS:
            raise MailError("AI mail category entry has an invalid schema")
        category = raw["category"]
        count = raw["count"]
        if (
            category not in MAIL_CATEGORIES
            or category in seen_categories
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            raise MailError("AI mail category entry has an invalid value")
        seen_categories.add(category)
        categories.append(
            {
                "category": category,
                "count": count,
                "summary": _bounded_text(
                    raw["summary"], f"categories[{index}].summary", limit=700
                ),
            }
        )
    if expected_count is not None and sum(item["count"] for item in categories) != expected_count:
        raise MailError("AI mail category counts do not cover the exact batch")
    raw_important = value["important"]
    if not isinstance(raw_important, list) or len(raw_important) > max_important:
        raise MailError("AI mail summary selected too many important messages")
    important: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_important):
        if not isinstance(raw, dict) or set(raw) != MAIL_IMPORTANT_KEYS:
            raise MailError("AI important-mail entry has an invalid schema")
        message_id = _bounded_text(raw["message_id"], f"important[{index}].message_id", limit=512)
        assert message_id is not None
        if message_id not in allowed_message_ids or message_id in seen:
            raise MailError("AI mail summary selected an unknown or duplicate message_id")
        seen.add(message_id)
        important.append(
            {
                "message_id": message_id,
                "summary": _bounded_text(raw["summary"], f"important[{index}].summary", limit=900),
                "why_important": _bounded_text(
                    raw["why_important"], f"important[{index}].why_important", limit=600
                ),
                "action": _bounded_text(
                    raw["action"], f"important[{index}].action", limit=500, nullable=True
                ),
                "deadline": _bounded_text(
                    raw["deadline"], f"important[{index}].deadline", limit=200, nullable=True
                ),
            }
        )
    return {"overview": overview, "categories": categories, "important": important}


def refresh_missing_bodies(
    conn: sqlite3.Connection,
    *,
    scheduled_at: str,
    runner: Callable[..., CommandResult] = run_mail_json,
    limit: int = 100,
) -> dict[str, Any]:
    """Backfill the same unassigned backlog that the summary will actually use."""
    end = parse_iso(scheduled_at)
    end_ms = int(end.timestamp() * 1000)
    rows = conn.execute(
        f"""SELECT mi.message_id,ie.event_pk
             FROM mail_items mi JOIN inbound_events ie
               ON ie.source='feishu_mail' AND ie.external_id=mi.message_id || ':received'
            WHERE cast(coalesce(mi.internal_date,'0') AS INTEGER)>0
              AND cast(coalesce(mi.internal_date,'0') AS INTEGER)<=?
              AND {UNASSIGNED_MAIL_SQL}
              AND coalesce(json_extract(ie.payload_json,'$.body_plain_text'),'')=''
            ORDER BY cast(mi.internal_date AS INTEGER),mi.message_id LIMIT ?""",
        (end_ms, max(1, min(limit, 400))),
    ).fetchall()
    if not rows:
        return {"selected": 0, "refreshed": 0}
    message_ids = [str(row["message_id"]) for row in rows]
    response = runner(
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
    data = response.data if isinstance(response.data, dict) else {}
    messages = data.get("messages") or []
    if not isinstance(messages, list):
        raise MailError("mail body backfill returned no messages list")
    by_id = {
        str(item.get("message_id")): item
        for item in messages
        if isinstance(item, dict) and item.get("message_id")
    }
    if set(by_id) != set(message_ids) or data.get("unavailable_message_ids"):
        raise MailError("mail body backfill did not return every exact message")
    from .lark import normalize_mail_event

    normalized = {
        message_id: normalize_mail_event({"message": by_id[message_id]})["payload"]
        for message_id in message_ids
    }
    if any(not str(value.get("body_plain_text") or "").strip() for value in normalized.values()):
        raise MailError("mail body backfill returned an empty plaintext body")
    with transaction(conn):
        for row in rows:
            message_id = str(row["message_id"])
            conn.execute(
                "UPDATE inbound_events SET payload_json=? WHERE event_pk=?",
                (canonical_json(normalized[message_id]), row["event_pk"]),
            )
    return {"selected": len(rows), "refreshed": len(rows)}


def _summary_input(
    rows: list[sqlite3.Row], *, max_body_chars: int, max_input_chars: int,
    envelope_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for row in rows:
        raw_payload: dict[str, Any] = {}
        try:
            candidate = json.loads(row["payload_json"] or "{}")
            if isinstance(candidate, dict):
                raw_payload = candidate
        except (TypeError, json.JSONDecodeError):
            pass
        body = raw_payload.get("body_plain_text") or raw_payload.get("body_preview") or row["body_preview"] or ""
        items.append(
            {
                "message_id": str(row["message_id"]),
                "sender": row["sender_name"] or row["sender_address"] or "未知发件人",
                "subject": row["subject"] or "（无主题）",
                "body_plain_text": str(body),
                "priority": raw_payload.get("priority_text") or raw_payload.get("priority_type"),
                "security_level": raw_payload.get("security_level"),
                "classification": row["classification"] or "information",
                "received_at": row["internal_date"],
            }
        )
    metadata_only = [{**item, "body_plain_text": ""} for item in items]
    envelope_metadata = dict(envelope_metadata or {})
    base_size = len(canonical_json({"messages": metadata_only, **envelope_metadata}))
    if base_size > max_input_chars:
        raise MailError("mail batch metadata exceeds the AI input safety limit")
    per_message = min(
        max_body_chars,
        max(0, (max_input_chars - base_size) // max(1, len(items))),
    )
    for item in items:
        item["body_plain_text"] = item["body_plain_text"][:per_message]
    value = {"messages": items, **envelope_metadata}
    if len(canonical_json(value)) > max_input_chars:
        # JSON escapes can consume more characters than the source text. Fit
        # the actual serialized envelope, including membership and all flags.
        bodies = [item["body_plain_text"] for item in items]
        low, high = 0, per_message
        while low < high:
            midpoint = (low + high + 1) // 2
            for item, body in zip(items, bodies, strict=True):
                item["body_plain_text"] = body[:midpoint]
            if len(canonical_json(value)) <= max_input_chars:
                low = midpoint
            else:
                high = midpoint - 1
        for item, body in zip(items, bodies, strict=True):
            item["body_plain_text"] = body[:low]
    return value


def _format_ai_summary(
    summary: dict[str, Any], links: dict[str, str | None], *, range_end: str, item_count: int,
    timezone: str = "Asia/Shanghai",
) -> str:
    local_end = parse_iso(range_end).astimezone(ZoneInfo(timezone))
    lines = [
        f"飞书邮箱 AI 摘要（截至 {local_end.strftime('%Y-%m-%d %H:%M')}）",
        "",
        str(summary["overview"]),
        f"本次摘要包含 {item_count} 封邮件（可能包含延迟补收的邮件）。",
    ]
    if summary["categories"]:
        lines.extend(("", "分类概况"))
        for item in summary["categories"]:
            lines.append(
                f"- {CATEGORY_LABELS[item['category']]}（{item['count']}）：{item['summary']}"
            )
    important = summary["important"]
    if important:
        lines.extend(("", f"需要你关注（{len(important)}）"))
        for index, item in enumerate(important, 1):
            lines.append(f"{index}. {item['summary']}")
            lines.append(f"   原因：{item['why_important']}")
            if item["action"]:
                lines.append(f"   建议行动：{item['action']}")
            if item["deadline"]:
                lines.append(f"   截止：{item['deadline']}")
            link = links.get(item["message_id"])
            lines.append(f"   打开邮件：{link}" if link else "   打开邮件：链接生成失败，已记录待修复")
    else:
        lines.extend(("", "本周期没有需要你立即处理的重要邮件。"))
    return "\n".join(lines)


def _finalize_digest_locked(conn: sqlite3.Connection, digest_id: str) -> dict[str, Any]:
    from .mail_preview import summary_entry_button

    run = conn.execute(
        "SELECT * FROM mail_digest_runs WHERE digest_id=?", (digest_id,)
    ).fetchone()
    if run is None:
        raise MailError("mail digest does not exist")
    if run["telegram_outbox_id"] or (run["notification_channel"] == "web" and run["state"] == "prepared"):
        return dict(run)
    link_rows = conn.execute(
        "SELECT * FROM mail_digest_links WHERE digest_id=? ORDER BY ordinal", (digest_id,)
    ).fetchall()
    if any(row["state"] in {"pending", "shared"} for row in link_rows):
        return dict(run)
    summary = json.loads(run["ai_summary_json"])
    links = {str(row["message_id"]): row["message_app_link"] for row in link_rows}
    text = _format_ai_summary(
        summary, links, range_end=str(run["range_end"]), item_count=int(run["item_count"]),
        timezone=str(run["timezone"]),
    )
    if run["notification_channel"] == "web":
        # Ready in the authenticated console is not a delivery receipt. Keep
        # the delivery watermark unchanged; frozen membership prevents repeats.
        conn.execute("UPDATE mail_digest_runs SET state='prepared' WHERE digest_id=?", (digest_id,))
        return dict(conn.execute("SELECT * FROM mail_digest_runs WHERE digest_id=?", (digest_id,)).fetchone())
    payload = {
        "text": text, "summary_id": digest_id, "range_end": run["range_end"],
        "watermark_key": run["watermark_key"], "mail_membership_digest": run["membership_digest"],
    }
    if run["notification_channel"] == "telegram":
        payload["buttons"] = [summary_entry_button(digest_id, run["membership_digest"])]
    else:
        payload["text"] = "新的邮件摘要已生成，请在网页控制台的邮件页查看。"
    outbox_id, _ = enqueue_outbox(
        conn, channel=run["notification_channel"], action_type="mail_summary",
        destination=str(run["telegram_destination"]), payload=payload,
        idempotency_key=f"mail-summary:{run['summary_type']}:{run['range_end']}",
    )
    now = iso_now()
    conn.execute(
        """INSERT OR IGNORE INTO summary_runs(summary_id,summary_type,watermark_key,range_start,
               range_end,item_count,content_digest,outbox_id,state,created_at)
           VALUES(?,?,?,?,?,?,?,?, 'prepared',?)""",
        (
            digest_id, run["summary_type"], run["watermark_key"], run["range_start"],
            run["range_end"], run["item_count"], digest(text), outbox_id, now,
        ),
    )
    conn.execute(
        "UPDATE mail_digest_runs SET state='prepared',telegram_outbox_id=? WHERE digest_id=?",
        (outbox_id, digest_id),
    )
    return dict(conn.execute("SELECT * FROM mail_digest_runs WHERE digest_id=?", (digest_id,)).fetchone())


def prepare_summary(
    conn: sqlite3.Connection,
    *,
    scheduled_at: str,
    destination: str,
    slot: str,
    notification_channel: str = "telegram",
    summarizer: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    share_destination: str | None = None,
    max_important: int = 8,
    max_body_chars: int = 6000,
    max_input_chars: int = 180000,
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    if notification_channel not in ("telegram", "feishu_im", "web"):
        raise MailError("unsupported mail notification channel")
    if not isinstance(destination, str) or not destination.strip():
        raise MailError("mail summary requires a destination")
    end = parse_iso(scheduled_at)
    if end.astimezone().tzinfo is None:
        raise MailError("scheduled_at must include timezone")
    if slot not in {"mail_noon", "mail_evening"}:
        raise MailError("invalid mail summary slot")
    ZoneInfo(timezone)
    watermark_key = "mail_summary_delivered"
    watermark = conn.execute("SELECT value_json FROM watermarks WHERE watermark_key=?", (watermark_key,)).fetchone()
    start = json.loads(watermark[0]).get("range_end") if watermark else None
    end_ms = int(end.timestamp() * 1000)
    existing = conn.execute(
        "SELECT * FROM mail_digest_runs WHERE summary_type=? AND range_end=?",
        (slot, end.isoformat()),
    ).fetchone()
    if existing is not None:
        return dict(existing)
    # Select rows and their category decisions in one read snapshot. Release it
    # before model/network work, then freeze these exact members at creation.
    savepoint = new_id("digest_read")
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        rows = conn.execute(
        f"""SELECT mi.*,ie.payload_json FROM mail_items mi
           LEFT JOIN inbound_events ie ON ie.source='feishu_mail'
             AND ie.external_id=mi.message_id || ':received'
           WHERE cast(coalesce(mi.internal_date,'0') AS INTEGER)>0
             AND cast(coalesce(mi.internal_date,'0') AS INTEGER)<=?
             AND {UNASSIGNED_MAIL_SQL}
           ORDER BY cast(mi.internal_date AS INTEGER),mi.message_id""",
        (end_ms,),
        ).fetchall()
        members = capture_members(conn, rows)
    finally:
        conn.execute(f"RELEASE {savepoint}")
    if rows:
        if summarizer is None:
            raise MailError("non-empty mail summary requires an AI summarizer")
        category_membership = [
            {"message_id": member["message_id"], "category": member["category"],
             "attention": member["attention"]} for member in members
        ]
        model_input = _summary_input(
            rows, max_body_chars=max_body_chars, max_input_chars=max_input_chars,
            envelope_metadata={"max_important": max_important, "category_membership": category_membership},
        )
        raw_summary = summarizer(model_input)
        if raw_summary is None:
            raise MailError("AI mail summarizer failed; watermark was not advanced")
        summary = validate_ai_summary(
            raw_summary,
            allowed_message_ids={str(row["message_id"]) for row in rows},
            max_important=max_important,
            expected_count=len(rows),
        )
    else:
        summary = {
            "overview": "本周期没有新邮件。",
            "categories": [],
            "important": [],
        }
    summary = bind_category_counts(summary, members)
    if notification_channel == "telegram" and summary["important"] and not share_destination:
        raise MailError("important mail links require summary_share_chat_id")
    summary_id = new_id("mdg")
    now = iso_now()
    with transaction(conn):
        # Different summary slots can race while their model calls run. A
        # competing reservation invalidates this entire model result; never
        # drop a few members and keep an explanation about a different set.
        if any(conn.execute(
            "SELECT 1 FROM mail_summary_membership WHERE message_id=?", (member["message_id"],)
        ).fetchone() for member in members):
            raise MailError("mail summary membership changed during analysis; retry from a fresh snapshot")
        cursor = conn.execute(
            """INSERT OR IGNORE INTO mail_digest_runs(digest_id,summary_type,watermark_key,
                   range_start,range_end,item_count,ai_summary_json,content_digest,
                   telegram_destination,notification_channel,state,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,'linking',?)""",
            (
                summary_id, slot, watermark_key, start, end.isoformat(), len(rows),
                canonical_json(summary), digest(canonical_json(summary)), destination, notification_channel, now,
            ),
        )
        if cursor.rowcount == 0:
            existing = conn.execute(
                "SELECT * FROM mail_digest_runs WHERE summary_type=? AND range_end=?", (slot, end.isoformat())
            ).fetchone()
            return dict(existing)
        freeze_members(conn, summary_id, members)
        conn.execute("UPDATE mail_digest_runs SET timezone=? WHERE digest_id=?", (timezone, summary_id))
        for ordinal, item in enumerate(summary["important"] if notification_channel == "telegram" else []):
            outbox_id, _ = enqueue_outbox(
                conn,
                channel="mail",
                action_type="share_to_owner",
                destination=str(share_destination),
                payload={"digest_id": summary_id, "message_id": item["message_id"]},
                idempotency_key=f"mail-digest-share:{summary_id}:{item['message_id']}",
            )
            conn.execute(
                """INSERT INTO mail_digest_links(digest_id,message_id,ordinal,share_outbox_id,
                       state,created_at,updated_at) VALUES(?,?,?,?, 'pending',?,?)""",
                (summary_id, item["message_id"], ordinal, outbox_id, now, now),
            )
        run = _finalize_digest_locked(conn, summary_id)
    result = dict(run)
    result.update(
        {"summary_id": summary_id, "range_start": start, "range_end": end.isoformat(), "item_count": len(rows)}
    )
    if result.get("telegram_outbox_id"):
        result["outbox_id"] = result["telegram_outbox_id"]
        row = conn.execute(
            "SELECT payload_json FROM outbox WHERE outbox_id=?", (result["telegram_outbox_id"],)
        ).fetchone()
        if row:
            result["text"] = json.loads(row[0])["text"]
    return result


def complete_mail_outbox_delivery(
    conn: sqlite3.Connection,
    *,
    outbox_id: str,
    action_type: str,
    remote_message_id: str,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Advance the durable share -> AppLink -> Telegram digest chain."""
    now = iso_now()
    if action_type == "share_to_owner":
        link = conn.execute(
            "SELECT * FROM mail_digest_links WHERE share_outbox_id=?", (outbox_id,)
        ).fetchone()
        if link is None:
            raise MailError("mail share outbox is not linked to a digest")
        resolver_id, _ = enqueue_outbox(
            conn,
            channel="mail",
            action_type="resolve_app_link",
            destination=remote_message_id,
            payload={"digest_id": link["digest_id"], "message_id": link["message_id"]},
            idempotency_key=f"mail-digest-link:{link['digest_id']}:{link['message_id']}",
        )
        conn.execute(
            """UPDATE mail_digest_links SET state='shared',im_message_id=?,resolve_outbox_id=?,
                   updated_at=? WHERE share_outbox_id=?""",
            (remote_message_id, resolver_id, now, outbox_id),
        )
        return None
    if action_type == "resolve_app_link":
        link = conn.execute(
            "SELECT * FROM mail_digest_links WHERE resolve_outbox_id=?", (outbox_id,)
        ).fetchone()
        if link is None:
            raise MailError("mail link resolver is not linked to a digest")
        app_link = result.get("message_app_link")
        if not isinstance(app_link, str) or not app_link.startswith("https://"):
            raise MailError("mail link resolver returned no HTTPS AppLink")
        conn.execute(
            """UPDATE mail_digest_links SET state='delivered',message_app_link=?,error=NULL,
                   updated_at=? WHERE resolve_outbox_id=?""",
            (app_link, now, outbox_id),
        )
        return _finalize_digest_locked(conn, str(link["digest_id"]))
    return None


def fail_mail_outbox(
    conn: sqlite3.Connection, *, outbox_id: str, action_type: str, error: str
) -> dict[str, Any] | None:
    column = "share_outbox_id" if action_type == "share_to_owner" else "resolve_outbox_id"
    if action_type not in {"share_to_owner", "resolve_app_link"}:
        return None
    link = conn.execute(
        f"SELECT digest_id FROM mail_digest_links WHERE {column}=?", (outbox_id,)
    ).fetchone()
    if link is None:
        return None
    conn.execute(
        f"UPDATE mail_digest_links SET state='failed',error=?,updated_at=? WHERE {column}=?",
        (error[:1000], iso_now(), outbox_id),
    )
    return _finalize_digest_locked(conn, str(link["digest_id"]))


def commit_summary_delivery(
    conn: sqlite3.Connection, *, outbox_id: str, remote_message_id: str
) -> bool:
    now = iso_now()
    with _tx(conn):
        summary = conn.execute("SELECT * FROM summary_runs WHERE outbox_id=?", (outbox_id,)).fetchone()
        outbox = conn.execute("SELECT state,remote_message_id FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        if summary is None or outbox is None or outbox["state"] != "delivered":
            raise MailError("summary watermark requires a delivered outbox")
        if summary["state"] == "delivered":
            return False
        conn.execute(
            "UPDATE summary_runs SET state='delivered',remote_message_id=?,delivered_at=? WHERE summary_id=?",
            (remote_message_id, now, summary["summary_id"]),
        )
        conn.execute(
            """UPDATE mail_digest_runs SET state='delivered',delivered_at=?
               WHERE digest_id=? AND telegram_outbox_id=?""",
            (now, summary["summary_id"], outbox_id),
        )
        watermark = conn.execute(
            "SELECT value_json FROM watermarks WHERE watermark_key=?",
            (summary["watermark_key"],),
        ).fetchone()
        current_end = (
            json.loads(watermark["value_json"]).get("range_end") if watermark else None
        )
        if current_end is None or parse_iso(current_end) < parse_iso(summary["range_end"]):
            conn.execute(
                """INSERT INTO watermarks(watermark_key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(watermark_key) DO UPDATE SET
                   value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (
                    summary["watermark_key"],
                    canonical_json(
                        {
                            "range_end": summary["range_end"],
                            "summary_id": summary["summary_id"],
                        }
                    ),
                    now,
                ),
            )
            return True
    return False
