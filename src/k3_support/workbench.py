from __future__ import annotations

import hashlib
import html
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .ids import canonical_json
from .timeutil import parse_iso

ACTIVE_AI_STATES = {
    "answering",
    "investigating",
    "waiting_board",
    "board_testing",
    "waiting_push",
    "monitoring",
}
SLA_MINUTES = {"P0": 5, "P1": 15, "P2": 60, "P3": 240}

WORKBENCH_LABELS = {
    "owner_decision": "待你判断",
    "approval": "待审批",
    "ai_processing": "AI 队列",
    "human_hold": "人工负责",
    "waiting": "等待",
    "case_error": "Case 异常",
    "knowledge_review": "知识复审",
    "delivery_failure": "投递失败",
    "mail_link_failure": "邮件链接失败",
    "job_failure": "任务失败",
    "closed_case": "已结束",
    "answered_faq": "资料咨询已答",
}
HISTORY_KINDS = ("closed_case", "answered_faq")
WORKBENCH_VIEWS = {
    "all": tuple(kind for kind in WORKBENCH_LABELS if kind not in HISTORY_KINDS),
    "needs_me": ("owner_decision", "approval", "case_error"),
    "ai": ("ai_processing",),
    "human": ("human_hold",),
    "waiting": ("waiting",),
    "errors": ("case_error", "job_failure", "delivery_failure", "mail_link_failure"),
    "approvals": ("approval",),
    "knowledge": ("knowledge_review",),
    "closed": HISTORY_KINDS,
}

# A display-only completion projection, never Case resolution or authority.
# The successful reply must cover the latest input and the *current* fence and
# round. A durable but not-yet-projected follow-up also keeps the Case visible.
_ANSWERED_FAQ_SQL = """c.type='faq' AND c.state='monitoring'
    AND c.outcome='answered' AND c.outcome_provenance='delivery_receipt'
    AND t.state='ai_sent'
    AND EXISTS(SELECT 1 FROM outbox done WHERE done.case_id=c.case_id
        AND done.lifecycle_round=c.lifecycle_round AND done.turn_id=t.turn_id
        AND done.source_event_pk=t.source_event_pk AND done.turn_revision=t.revision
        AND done.communication_fence=t.fence AND done.channel='feishu_im'
        AND done.action_type='reply' AND done.state='delivered'
        AND length(done.remote_message_id)>0 AND done.delivered_at IS NOT NULL)
    AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.case_id=c.case_id
        AND j.lifecycle_round=c.lifecycle_round AND j.state NOT IN ('succeeded','cancelled'))
    AND NOT EXISTS(SELECT 1 FROM approvals a WHERE a.case_id=c.case_id
        AND a.lifecycle_round=c.lifecycle_round AND a.status IN ('requested','approved')
        AND (julianday(a.expires_at)>julianday(:now) OR julianday(a.expires_at) IS NULL))
    AND NOT EXISTS(SELECT 1 FROM outbox o WHERE o.case_id=c.case_id
        AND o.lifecycle_round=c.lifecycle_round
        AND o.state IN ('pending','retry','sending','permanent_failure'))
    AND NOT EXISTS(SELECT 1 FROM outbox_attempts attempt JOIN outbox o USING(outbox_id)
        WHERE o.case_id=c.case_id AND attempt.dispatch_started_at IS NOT NULL
          AND NOT EXISTS(SELECT 1 FROM outbox_attempt_events receipt
              WHERE receipt.claim_token=attempt.claim_token AND receipt.event_type IN ('delivered','failed')))
    AND NOT EXISTS(SELECT 1 FROM locks WHERE case_id=c.case_id)
    AND NOT EXISTS(SELECT 1 FROM inbound_events incoming
        WHERE incoming.source IN ('feishu_bot_im','feishu_user_poll')
          AND incoming.status IN ('new','claimed','dead_letter')
          AND (incoming.event_pk=t.source_event_pk OR (
            julianday(incoming.received_at)>=(SELECT julianday(started_at) FROM case_rounds
                WHERE case_id=c.case_id AND round_number=c.lifecycle_round)
            AND (EXISTS(SELECT 1 FROM case_events ce WHERE ce.case_id=c.case_id
                        AND ce.source_event_pk=incoming.event_pk)
                 OR EXISTS(SELECT 1 FROM conversation_turns related WHERE related.case_id=c.case_id
                           AND related.source_event_pk=incoming.event_pk)
                 OR (incoming.chat_id=t.chat_id
                     AND ((t.chat_type='p2p' AND incoming.sender_id=c.requester_id)
                          OR (t.chat_type='group' AND (
                          incoming.thread_id IN (t.thread_id,t.root_message_id,t.source_message_id)
                          OR json_extract(incoming.payload_json,'$.root_id') IN (t.thread_id,t.root_message_id,t.source_message_id)
                          OR json_extract(incoming.payload_json,'$.parent_id') IN (t.thread_id,t.root_message_id,t.source_message_id)
                          OR json_extract(incoming.payload_json,'$.reply_to') IN (t.thread_id,t.root_message_id,t.source_message_id)
                          ))
                     )
                 )
            )
          )))
"""

_QUEUE_SQL = """WITH queue AS (
    SELECT 'case:'||c.case_id AS item_id,c.case_id AS target_id,
           CASE WHEN c.state IN ('resolved','cancelled') THEN 'closed_case'
                WHEN c.state='paused' THEN 'waiting'
                WHEN ACTIVE_DELIVERY_BLOCK THEN 'owner_decision'
                WHEN c.state='error' THEN 'case_error'
                WHEN c.state='takeover' OR c.owner='operator'
                     OR t.communication_owner='human' THEN 'human_hold'
                WHEN c.state='escalated' THEN 'owner_decision'
                WHEN ANSWERED_FAQ THEN 'answered_faq'
                WHEN c.state IN ('paused','waiting_board','waiting_push','monitoring') THEN 'waiting'
                ELSE 'ai_processing' END AS kind,
           c.case_id,CASE_TITLE AS title,c.severity,c.state,PROGRESS_AT AS progress_at,
           cast(c.version AS TEXT) AS revision,
           coalesce(t.turn_id,'')||':'||coalesce(t.revision,0)||':'||coalesce(t.fence,0) AS authority_revision,
           CASE_NEXT_ACTION AS next_action,'case' AS entity_kind,c.case_id AS target_key
      FROM cases c LEFT JOIN conversation_turns t ON t.turn_id=(
          SELECT turn_id FROM conversation_turns WHERE case_id=c.case_id
           ORDER BY created_at DESC,rowid DESC LIMIT 1)
    UNION ALL
    SELECT 'approval:'||approval_id,approval_id,'approval',case_id,
           approval_type,NULL,status,created_at,action_digest,expires_at,NULL,'approval',approval_id
      FROM approvals WHERE status='requested' AND julianday(expires_at)>julianday(:now)
    UNION ALL
    SELECT 'knowledge:'||knowledge_id,knowledge_id,'knowledge_review',NULL,
           title,NULL,status,updated_at,content_digest,source_digest,NULL,'knowledge',knowledge_id
      FROM knowledge_entries WHERE status IN ('candidate','stale')
    UNION ALL
    SELECT 'outbox:'||outbox_id,outbox_id,'delivery_failure',case_id,
           channel||'/'||action_type,NULL,state,updated_at,OUTBOX_REVISION,'',NULL,'outbox',outbox_id
      FROM outbox WHERE state='permanent_failure'
       AND (channel<>'feishu_urgent_app' OR :app_enabled)
       AND (channel<>'feishu_urgent_sms' OR :sms_enabled)
    UNION ALL
    SELECT 'job:'||job_id,job_id,'job_failure',case_id,
           job_type||'：'||CASE WHEN JOB_CONTENT_RETIRED THEN '历史内容已清理，仅保留任务状态'
               WHEN error_class='broker_input_invalid'
               THEN '输入快照异常，等待处理'
               WHEN error_class='broker_service_failed' THEN '编码服务异常退出，等待核对；未自动重试'
               WHEN error_class='broker_report_unavailable' THEN '编码服务已退出，但报告缺失或校验失败；等待核对'
               WHEN error_class='broker_budget_blocked' THEN '预算阻断，编码未启动；核对额度和未知费用后人工恢复，不要清空占用'
               ELSE coalesce(error_class,'未记录原因') END,NULL,state,updated_at,
           coalesce(output_digest,''),'',NULL,'job',job_id
      FROM jobs WHERE state IN ('failed','orphaned')
         OR (state='waiting' AND error_class IN ('broker_input_invalid','broker_report_unavailable','broker_budget_blocked'))
    UNION ALL
    SELECT 'mail:'||json_array(digest_id,message_id),json_array(digest_id,message_id),
           'mail_link_failure',NULL,'重要邮件原文链接',NULL,state,updated_at,
           coalesce(message_app_link,''),'',NULL,'mail',json_array(digest_id,message_id)
      FROM mail_digest_links WHERE state='failed'
)
"""


def waiting_for(state: str, kind: str) -> str:
    if kind == "answered_faq":
        return "资料咨询已答；无需确认，不代表现场解决"
    if kind == "closed_case":
        return "已结束；仅人工确认的解决才代表结案"
    if kind == "human_hold":
        return "你（人工负责）"
    if kind == "owner_decision":
        return "你决策"
    if kind == "approval":
        return "你审核；查看详情不会批准"
    if kind in {"case_error", "delivery_failure", "job_failure", "mail_link_failure"}:
        return "错误处置；未自动重试外部操作"
    if kind == "knowledge_review":
        return "知识审核"
    return {
        "waiting_board": "确认 board1 占用",
        "waiting_push": "审核 WIP push",
        "paused": "人工暂停",
        "monitoring": "验证结果（尚未确认）",
        "intake": "消息分流",
        "triage": "分流处理",
    }.get(state, "AI 处理")


def _age_minutes(value: str, now: datetime) -> int:
    return max(0, int((now - parse_iso(value).astimezone(UTC)).total_seconds() // 60))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _queue_query(conn: sqlite3.Connection) -> str:
    lifecycle = _column_exists(conn, "cases", "last_material_progress_at")
    has_blocks = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='active_delivery_blocks'"
        ).fetchone()
        is not None
    )
    block = (
        "EXISTS(SELECT 1 FROM active_delivery_blocks b WHERE b.case_id=c.case_id)"
        if has_blocks
        else "0"
    )
    recorded_action = (
        "CASE WHEN c.type='faq' AND c.state='monitoring' AND c.outcome='answered' "
        "AND NOT (" + _ANSWERED_FAQ_SQL + ") THEN "
        "'资料咨询已有答复，但还有新输入、未完成事项或答复依据待确认；请查看当前消息与任务。' "
        "ELSE c.next_action END"
        if lifecycle
        else "c.next_action"
    )
    next_action = (
        "coalesce((SELECT next_action FROM active_delivery_blocks b WHERE b.case_id=c.case_id "
        "ORDER BY created_at DESC,outbox_id DESC LIMIT 1)," + recorded_action + ")"
        if has_blocks
        else recorded_action
    )
    has_retirement = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='case_content_retirements'").fetchone() is not None
    retired = ("EXISTS(SELECT 1 FROM case_content_retirements retired WHERE retired.case_id=c.case_id)"
               if has_retirement else '0')
    outbox_revision = ("CASE WHEN EXISTS(SELECT 1 FROM case_content_retirements retired "
                       "WHERE retired.case_id=outbox.case_id AND retired.lifecycle_round=outbox.lifecycle_round) "
                       "THEN 'retired:'||outbox_id||':'||updated_at ELSE payload_json END"
                       if has_retirement else 'payload_json')
    job_retired = ("EXISTS(SELECT 1 FROM case_content_retirements retired "
                   "WHERE retired.case_id=jobs.case_id AND retired.lifecycle_round=jobs.lifecycle_round)"
                   if has_retirement else '0')
    title = f"CASE WHEN {retired} THEN '历史内容已清理' ELSE c.title END"
    next_action = f"CASE WHEN {retired} THEN '仅可查看保留元数据；旧内容不可恢复。' ELSE ({next_action}) END"
    return (
        _QUEUE_SQL.replace(
            "PROGRESS_AT",
            "coalesce(c.last_material_progress_at,c.updated_at)"
            if lifecycle
            else "c.updated_at",
        )
        .replace("ACTIVE_DELIVERY_BLOCK", block)
        .replace("ANSWERED_FAQ", _ANSWERED_FAQ_SQL if lifecycle else "0")
        .replace("CASE_NEXT_ACTION", next_action)
        .replace("CASE_TITLE", title)
        .replace("OUTBOX_REVISION", outbox_revision)
        .replace("JOB_CONTENT_RETIRED", job_retired)
    )


def _queue_params(config: Config | None, observed: datetime) -> dict[str, Any]:
    return {
        "now": observed.isoformat(),
        "app_enabled": config is None or config.notification("feishu_app_urgent"),
        "sms_enabled": config is None or config.notification("feishu_sms_urgent"),
    }


def _counts(
    conn: sqlite3.Connection, query: str, params: dict[str, Any]
) -> dict[str, int]:
    counts = dict.fromkeys(WORKBENCH_LABELS, 0)
    counts.update(
        {
            str(row[0]): int(row[1])
            for row in conn.execute(
                query + "SELECT kind,count(*) FROM queue GROUP BY kind", params
            )
        }
    )
    counts["overdue"] = int(
        conn.execute(
            query
            + """SELECT count(*) FROM queue WHERE kind='ai_processing'
            AND (julianday(:now)-julianday(progress_at))*1440 >=
                CASE severity WHEN 'P0' THEN 5 WHEN 'P1' THEN 15
                              WHEN 'P2' THEN 60 ELSE 240 END""",
            params,
        ).fetchone()[0]
    )
    return counts


def workbench_counts(
    conn: sqlite3.Connection, *, config: Config | None = None
) -> dict[str, int]:
    """The same classification as the workbench, without generating pages."""
    conn.execute("SAVEPOINT workbench_counts_read")
    try:
        return _counts(
            conn, _queue_query(conn), _queue_params(config, datetime.now(UTC))
        )
    finally:
        conn.execute("RELEASE workbench_counts_read")


def case_workbench_status(conn: sqlite3.Connection, *, case_id: str) -> dict[str, Any]:
    row = conn.execute(
        _queue_query(conn)
        + "SELECT kind,next_action FROM queue WHERE item_id=:item_id",
        {**_queue_params(None, datetime.now(UTC)), "item_id": f"case:{case_id}"},
    ).fetchone()
    if row is None:
        raise ValueError("workbench Case not found")
    result = dict(row)
    if (
        result["kind"] == "owner_decision"
        and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='active_delivery_blocks'"
        ).fetchone()
    ):
        block = conn.execute(
            "SELECT reason FROM active_delivery_blocks WHERE case_id=? ORDER BY created_at DESC,outbox_id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if block:
            result["block_reason"] = block["reason"]
    return result


def workbench_snapshot(
    conn: sqlite3.Connection,
    *,
    config: Config | None = None,
    now: datetime | None = None,
    limit: int = 12,
    page: int = 1,
    view: str = "all",
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Legacy numeric report API; the mobile UI uses workbench_page keysets."""
    if view not in WORKBENCH_VIEWS:
        raise ValueError("unknown workbench view")
    if isinstance(page, bool) or page < 1 or page > 99999:
        raise ValueError("workbench page is out of range")
    limit = max(1, min(limit, 50))
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    query = _queue_query(conn)
    params = _queue_params(config, observed)
    conn.execute("SAVEPOINT workbench_read")
    try:
        counts = _counts(conn, query, params)
        fingerprint = hashlib.sha256()
        for row in conn.execute(query + "SELECT * FROM queue ORDER BY item_id", params):
            fingerprint.update(canonical_json(dict(row)).encode())
            fingerprint.update(b"\n")
        snapshot_digest = fingerprint.hexdigest()[:16]
        if expected_digest is not None and expected_digest != snapshot_digest:
            raise ValueError("workbench snapshot is stale; refresh the workbench")
        kinds = WORKBENCH_VIEWS[view]
        where = (
            ""
            if not kinds
            else " WHERE kind IN (" + ",".join(repr(kind) for kind in kinds) + ")"
        )
        total = int(
            conn.execute(
                query + "SELECT count(*) FROM queue" + where, params
            ).fetchone()[0]
        )
        page_count = max(1, (total + limit - 1) // limit)
        if page > page_count:
            raise ValueError("workbench page is out of range")
        rows = conn.execute(
            query
            + "SELECT * FROM queue"
            + where
            + """
            ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                                   WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 ELSE 4 END,
                     CASE kind WHEN 'owner_decision' THEN 0 WHEN 'case_error' THEN 1
                               WHEN 'approval' THEN 2 WHEN 'delivery_failure' THEN 3
                               WHEN 'job_failure' THEN 4 ELSE 5 END,
                     progress_at,item_id LIMIT :limit OFFSET :offset""",
            {**params, "limit": limit, "offset": (page - 1) * limit},
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item.pop("revision", None)
            item.pop("authority_revision", None)
            item["title_truncated"] = len(item["title"]) > 300
            item["title"] = item["title"][:300]
            item["age_minutes"] = _age_minutes(item["progress_at"], observed)
            item["overdue"] = item["kind"] == "ai_processing" and item[
                "age_minutes"
            ] >= SLA_MINUTES.get(item["severity"], 240)
            item["waiting_for"] = waiting_for(item["state"], item["kind"])
            for kind, key in (
                ("approval", "approval_id"),
                ("knowledge_review", "knowledge_id"),
                ("delivery_failure", "outbox_id"),
                ("job_failure", "job_id"),
            ):
                if item["kind"] == kind:
                    item[key] = item["target_id"]
            items.append(item)
        return {
            "generated_at": observed.isoformat(),
            "counts": counts,
            "items": items,
            "total_items": total,
            "all_items": sum(
                counts[kind] for kind in WORKBENCH_LABELS if kind not in HISTORY_KINDS
            ),
            "truncated": total > limit,
            "page": page,
            "page_count": page_count,
            "limit": limit,
            "view": view,
            "snapshot_digest": snapshot_digest,
        }
    finally:
        conn.execute("RELEASE workbench_read")


def render_workbench(snapshot: dict[str, Any]) -> str:
    counts = snapshot["counts"]
    lines = [
        "待处理工作台",
        (
            f"待你判断 {counts['owner_decision']}｜待审批 {counts['approval']}｜"
            f"AI 处理中 {counts['ai_processing']}｜人工暂挂 {counts['human_hold']}"
        ),
        (
            f"知识复审 {counts['knowledge_review']}｜投递失败 {counts['delivery_failure']}｜"
            f"已超时 {counts['overdue']}"
        ),
    ]
    labels = WORKBENCH_LABELS
    lines.append(
        f"等待 {counts['waiting']}｜Case 异常 {counts['case_error']}｜任务失败 {counts['job_failure']}"
    )
    lines.append(f"资料咨询已答 {counts['answered_faq']}（不占待办，不代表现场解决）")
    lines.append(
        f"第 {snapshot['page']}/{snapshot['page_count']} 页 · 本视图共 {snapshot['total_items']} 项"
    )
    if snapshot["items"]:
        lines.append("")
    for item in snapshot["items"]:
        coordinate = (
            item.get("case_id") or item.get("knowledge_id") or item.get("outbox_id")
        )
        age = "" if item.get("age_minutes") is None else f" · {item['age_minutes']}分钟"
        overdue = " ⚠️超时" if item["overdue"] else ""
        lines.append(
            f"- [{labels[item['kind']]}] {coordinate or item['target_id']} · {item['title'][:90]}{age}{overdue}"
        )
    if snapshot["truncated"]:
        lines.append(
            f"- 另有 {snapshot['total_items'] - len(snapshot['items'])} 项未展开"
        )
    return "\n".join(lines)


def _indexed_query(conn: sqlite3.Connection) -> str:
    return (
        _queue_query(conn)
        + """, indexed AS (
        SELECT q.*,k.item_seq FROM queue q JOIN workbench_item_keys k
          ON k.entity_kind=q.entity_kind AND k.target_key=q.target_key
    ) """
    )


def _view_where(view: str) -> str:
    if view not in WORKBENCH_VIEWS:
        raise ValueError("unknown workbench view")
    return "kind IN (" + ",".join(repr(kind) for kind in WORKBENCH_VIEWS[view]) + ")"


def _decorate_item(row: sqlite3.Row, observed: datetime) -> dict[str, Any]:
    item = dict(row)
    item.pop("revision", None)
    item.pop("authority_revision", None)
    item["title_truncated"] = len(item["title"]) > 300
    item["title"] = item["title"][:300]
    item["age_minutes"] = _age_minutes(item["progress_at"], observed)
    item["overdue"] = item["kind"] == "ai_processing" and item[
        "age_minutes"
    ] >= SLA_MINUTES.get(item["severity"], 240)
    item["waiting_for"] = waiting_for(item["state"], item["kind"])
    for kind, key in (
        ("approval", "approval_id"),
        ("knowledge_review", "knowledge_id"),
        ("delivery_failure", "outbox_id"),
        ("job_failure", "job_id"),
    ):
        if item["kind"] == kind:
            item[key] = item["target_id"]
    return item


def _short_display(value: str, budget: int) -> str:
    """Bound both escaped HTML and Telegram UTF-16 width; details stay complete."""
    width = 0
    for index, character in enumerate(value):
        width += max(
            len(html.escape(character)), len(character.encode("utf-16-le")) // 2
        )
        if width > budget:
            return value[:index] + "…"
    return value


def workbench_page(
    conn: sqlite3.Connection,
    config: Config | None = None,
    *,
    view: str = "all",
    cursor: str | None = None,
    limit: int = 4,
    render_buttons: bool = True,
) -> dict[str, Any]:
    """Live membership under an opening upper bound; read-only keyset navigation."""
    from dataclasses import replace

    from .workbench_navigation import (
        VIEW_CODES,
        decode,
        encode,
        open_navigation,
        validate_origin,
    )

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("workbench limit is out of range")
    if not isinstance(render_buttons, bool) or (render_buttons and limit > 4):
        raise ValueError(
            "Telegram workbench limit must be at most 4; use report presentation for larger reads"
        )
    conn.execute("SAVEPOINT workbench_page_read")
    try:
        if cursor is None:
            nav = open_navigation(conn, view)
        else:
            nav = decode(conn, validate_origin(conn, cursor))[0]
        origin = encode(nav)
        observed = datetime.now(UTC)
        query, params = _indexed_query(conn), _queue_params(config, observed)
        # An incomplete migration/corruption is an error, never a silent omitted item
        # or a reason to repair an index during a read-only interaction.
        missing = conn.execute(
            _queue_query(conn)
            + """SELECT 1 FROM queue q
            LEFT JOIN workbench_item_keys k ON k.entity_kind=q.entity_kind AND k.target_key=q.target_key
            WHERE k.item_seq IS NULL LIMIT 1""",
            params,
        ).fetchone()
        if missing:
            raise ValueError(
                "workbench identity index incomplete; trusted migration required"
            )
        counts = _counts(conn, _queue_query(conn), params)
        where = _view_where(nav.view) + " AND item_seq<=:upper"
        params.update(upper=nav.upper, anchor=nav.anchor, limit=limit + 1)
        comparison, order = ("<", "DESC") if nav.previous else (">", "ASC")
        rows = conn.execute(
            query
            + "SELECT * FROM indexed WHERE "
            + where
            + f" AND item_seq{comparison}:anchor ORDER BY item_seq {order} LIMIT :limit",
            params,
        ).fetchall()
        rows = rows[:limit]
        if nav.previous:
            rows.reverse()
        items = [_decorate_item(row, observed) for row in rows]
        total = int(
            conn.execute(
                query + "SELECT count(*) FROM indexed WHERE " + where, params
            ).fetchone()[0]
        )
        next_cursor = previous_cursor = None
        if rows:
            first, last = rows[0]["item_seq"], rows[-1]["item_seq"]
            if conn.execute(
                query
                + "SELECT 1 FROM indexed WHERE "
                + where
                + " AND item_seq<:edge LIMIT 1",
                {**params, "edge": first},
            ).fetchone():
                previous_cursor = encode(replace(nav, anchor=first, previous=True))
            if conn.execute(
                query
                + "SELECT 1 FROM indexed WHERE "
                + where
                + " AND item_seq>:edge LIMIT 1",
                {**params, "edge": last},
            ).fetchone():
                next_cursor = encode(replace(nav, anchor=last, previous=False))
        snapshot = {
            "generated_at": observed.isoformat(),
            "counts": counts,
            "items": items,
            "total_items": total,
            "all_items": sum(
                counts[kind] for kind in WORKBENCH_LABELS if kind not in HISTORY_KINDS
            ),
            "limit": limit,
            "view": nav.view,
            "cursor": origin,
            "upper": nav.upper,
            "next_cursor": next_cursor,
            "previous_cursor": previous_cursor,
            "has_next": next_cursor is not None,
            "has_previous": previous_cursor is not None,
            "ordering": "first_registered_ascending",
            "membership": "current_below_opening_upper_bound",
        }
        view_labels = {
            "all": "全部",
            "needs_me": "需要我",
            "ai": "AI 队列",
            "human": "人工负责",
            "waiting": "等待",
            "errors": "异常",
            "approvals": "审批",
            "knowledge": "知识",
            "closed": "已结束",
        }
        buttons = [
            {
                "text": ("✓ " if target == nav.view else "") + label,
                "callback_data": encode(
                    replace(nav, view=target, anchor=0, previous=False)
                ),
                "row": index // 3,
            }
            for index, (target, label) in enumerate(view_labels.items())
        ]
        lines = [
            "按首次登记排序，状态实时更新；刷新后纳入新事项。",
            f"本次打开范围内，本筛选当前 {total} 项；本页显示 {len(items)} 项。",
            f"全局实时：待你判断 {counts['owner_decision']}｜待审批 {counts['approval']}｜AI 队列 {counts['ai_processing']}｜人工负责 {counts['human_hold']}",
            f"资料咨询已答 {counts['answered_faq']}（不占待办，不代表现场解决）",
        ]
        invalid_times = int(
            conn.execute(
                "SELECT legacy_invalid_timestamps FROM workbench_navigation_namespace WHERE singleton=1"
            ).fetchone()[0]
        )
        if invalid_times:
            lines.append(
                f"历史 {invalid_times} 项登记时间无效，迁移时已按类型和标识确定性补排。"
            )
        for index, item in enumerate(items, 1):
            lines.extend(
                [
                    "",
                    f"{index}. [{WORKBENCH_LABELS[item['kind']]}] {_short_display(item['case_id'] or item['target_id'], 96)}",
                    f"{item['severity'] or '未定级'} · {_short_display(item['title'], 250)} · {item['age_minutes']}分钟"
                    + (" ⚠️超时" if item["overdue"] else ""),
                    f"等待：{item['waiting_for']}",
                ]
            )
            if item.get("next_action"):
                action = str(item["next_action"])
                lines.append("下一步：" + _short_display(action, 170))
            buttons.append(
                {
                    "text": f"{index}. {WORKBENCH_LABELS[item['kind']]}",
                    "callback_data": encode(nav, item_seq=item["item_seq"]),
                    "row": 3 + (index - 1) // 2,
                }
            )
        button_row = 3 + (len(items) + 1) // 2
        for target, label in ((previous_cursor, "上一页"), (next_cursor, "下一页")):
            if target:
                buttons.append(
                    {"text": label, "callback_data": target, "row": button_row}
                )
        if not rows and nav.anchor:
            lines.append("此段事项已离开原筛选，可返回开头继续查看；没有改指其他事项。")
            buttons.append(
                {
                    "text": "返回开头",
                    "callback_data": encode(replace(nav, anchor=0, previous=False)),
                    "row": button_row,
                }
            )
        buttons.append(
            {
                "text": "刷新（含新事项）",
                "callback_data": "wb2:open:" + VIEW_CODES[nav.view],
                "row": button_row,
            }
        )
        if items:
            lines.append("\n以上为简要预览；点击事项可看完整记录与约束。")
        return {
            "command": "workbench",
            "operation": "list",
            "snapshot": snapshot,
            "preview": {
                "text": "<b>支持工作台</b>\n只读查看，不改变当前模式。\n\n"
                + html.escape("\n".join(lines)),
                "parse_mode": "HTML",
                "buttons": buttons if render_buttons else [],
                "display_target": "telegram" if render_buttons else "report",
                "position_label": f"本页 {len(items)} 项 · 当前筛选 {total} 项",
            },
        }
    finally:
        conn.execute("RELEASE workbench_page_read")


def workbench_panel(
    conn: sqlite3.Connection,
    config: Config,
    *,
    page: int = 1,
    view: str = "all",
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Initial-menu adapter. Legacy page/digest buttons must not be reinterpreted."""
    if page != 1 or expected_digest is not None:
        raise ValueError(
            "legacy workbench navigation is unsupported; refresh the workbench"
        )
    return workbench_page(conn, config, view=view)


def workbench_item(
    conn: sqlite3.Connection, config: Config, **kwargs: Any
) -> dict[str, Any]:
    raise ValueError(
        "legacy ordinal workbench button is unsupported; refresh the workbench"
    )


def _source_item(
    conn: sqlite3.Connection, entity: str, target: str
) -> dict[str, Any] | None:
    import json

    fields = {
        "case": (
            "cases",
            "case_id",
            "title,severity,state,last_material_progress_at AS progress_at,case_id",
        ),
        "approval": (
            "approvals",
            "approval_id",
            "approval_type AS title,NULL AS severity,status AS state,created_at AS progress_at,case_id",
        ),
        "knowledge": (
            "knowledge_entries",
            "knowledge_id",
            "title,NULL AS severity,status AS state,updated_at AS progress_at,NULL AS case_id",
        ),
        "outbox": (
            "outbox",
            "outbox_id",
            "channel||'/'||action_type AS title,NULL AS severity,state,updated_at AS progress_at,case_id",
        ),
        "job": (
            "jobs",
            "job_id",
            "job_type AS title,NULL AS severity,state,updated_at AS progress_at,case_id",
        ),
    }
    if entity == "mail":
        digest_id, message_id = json.loads(target)
        row = conn.execute(
            """SELECT '邮件原文链接' AS title,NULL AS severity,state,
            updated_at AS progress_at,NULL AS case_id FROM mail_digest_links WHERE digest_id=? AND message_id=?""",
            (digest_id, message_id),
        ).fetchone()
    else:
        table, key, columns = fields[entity]
        row = conn.execute(
            f"SELECT {columns} FROM {table} WHERE {key}=?", (target,)
        ).fetchone()
    return dict(row) if row else None


def workbench_target(
    conn: sqlite3.Connection,
    config: Config | None = None,
    *,
    item_seq: int,
    origin_cursor: str,
    page: int = 1,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    from .workbench_navigation import decode, encode, validate_origin

    conn.execute("SAVEPOINT workbench_target_read")
    try:
        origin = validate_origin(conn, origin_cursor)
        nav = decode(conn, origin)[0]
        if (
            isinstance(item_seq, bool)
            or not isinstance(item_seq, int)
            or not 1 <= item_seq <= nav.upper
        ):
            raise ValueError("workbench target is out of range")
        key = conn.execute(
            "SELECT * FROM workbench_item_keys WHERE item_seq=?", (item_seq,)
        ).fetchone()
        if key is None:
            raise ValueError("unknown workbench target; refresh the workbench")
        params = {
            **_queue_params(config, datetime.now(UTC)),
            "entity": key["entity_kind"],
            "target": key["target_key"],
        }
        row = conn.execute(
            _queue_query(conn)
            + "SELECT * FROM queue WHERE entity_kind=:entity AND target_key=:target",
            params,
        ).fetchone()
        item = (
            dict(row)
            if row
            else _source_item(conn, key["entity_kind"], key["target_key"])
        )
        notice = (
            ""
            if row and row["kind"] in WORKBENCH_VIEWS[nav.view]
            else "已不在原筛选，以下为当前状态。\n"
        )
        if item is None:
            notice = "该事项已移除；旧按钮不会改指其他事项。\n"
            result = {
                "command": "workbench",
                "operation": "detail",
                "preview": {
                    "text": "<b>事项已移除</b>",
                    "parse_mode": "HTML",
                    "buttons": [],
                    "page": 1,
                    "page_count": 1,
                },
            }
        elif (
            item.get("case_id")
            and conn.execute(
                "SELECT 1 FROM cases WHERE case_id=?", (item["case_id"],)
            ).fetchone()
        ):
            from .case_detail import case_detail

            result = case_detail(
                conn,
                case_id=item["case_id"],
                page=page,
                expected_digest=expected_digest,
                origin_cursor=origin,
            )
        elif key["entity_kind"] == "knowledge":
            from .knowledge_preview import knowledge_preview

            result = knowledge_preview(
                conn,
                knowledge_id=key["target_key"],
                page=page,
                expected_digest=expected_digest,
            )
            preview = result["preview"]
            preview["buttons"] = [
                {
                    "text": label,
                    "row": 0,
                    "callback_data": encode(
                        nav,
                        item_seq=item_seq,
                        page=target,
                        content_digest=preview["content_digest"],
                    ),
                }
                for target, label in ((page - 1, "上一页"), (page + 1, "下一页"))
                if 1 <= target <= preview["page_count"]
            ]
            result.update(command="workbench", operation="detail")
        else:
            lines = [
                f"事项：{key['target_key']}",
                f"类型：{key['entity_kind']}",
                f"标题：{item['title']}",
                f"状态：{item['state']}",
                "尚未关联 Case；查看不会重试、批准或改变处理状态。",
            ]
            fingerprint = hashlib.sha256(canonical_json(item).encode()).hexdigest()[:16]
            if expected_digest is not None and expected_digest != fingerprint:
                raise ValueError("workbench detail is stale; refresh the workbench")
            if page != 1:
                raise ValueError("workbench detail page is out of range")
            result = {
                "command": "workbench",
                "operation": "detail",
                "preview": {
                    "text": "<b>事项详情</b>\n\n" + html.escape("\n".join(lines)),
                    "parse_mode": "HTML",
                    "buttons": [],
                    "page": 1,
                    "page_count": 1,
                    "content_digest": fingerprint,
                },
            }
        preview = result["preview"]
        preview["text"] = html.escape(notice) + preview["text"]
        preview["buttons"] = [
            button for button in preview["buttons"] if button["text"] != "返回工作台"
        ]
        preview["buttons"].append(
            {"text": "返回工作台", "callback_data": origin, "row": 1}
        )
        result["origin_cursor"] = origin
        result["item_seq"] = item_seq
        return result
    finally:
        conn.execute("RELEASE workbench_target_read")


def detail_page(
    conn: sqlite3.Connection,
    config: Config | None = None,
    *,
    item_seq: int,
    page: int,
    expected_digest: str,
    origin_cursor: str,
) -> dict[str, Any]:
    return workbench_target(
        conn,
        config,
        item_seq=item_seq,
        page=page,
        expected_digest=expected_digest,
        origin_cursor=origin_cursor,
    )


def professional_knowledge_snapshot(
    conn: sqlite3.Connection,
    *,
    repository_root: Path,
    now: datetime | None = None,
    issue_limit: int = 100,
) -> dict[str, Any]:
    """Build an auditable readiness view without changing repository or database state."""
    from .professional_knowledge import ProfessionalKnowledgeError, load_article
    from .source_refresh import source_refresh_snapshot

    if issue_limit < 1 or issue_limit > 1000:
        raise ValueError("issue_limit must be between 1 and 1000")
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    root = repository_root.expanduser().resolve()
    paths = (
        sorted((root / "articles").rglob("*.md"))
        if (root / "articles").is_dir()
        else []
    )
    states: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    components: Counter[str] = Counter()
    issues: list[dict[str, str]] = []
    total_issues = 0
    valid_articles = 0

    def add_issue(path: Path, stable_id: str, code: str, detail: str) -> None:
        nonlocal total_issues
        total_issues += 1
        if len(issues) < issue_limit:
            issues.append(
                {
                    "path": str(path.relative_to(root)),
                    "id": stable_id,
                    "code": code,
                    "detail": detail,
                }
            )

    for path in paths:
        try:
            article = load_article(path, now=observed)
        except (ProfessionalKnowledgeError, OSError) as exc:
            add_issue(path, "unknown", "schema_invalid", str(exc))
            continue
        valid_articles += 1
        metadata = article.metadata
        stable_id = str(metadata["id"])
        states[str(metadata["status"])] += 1
        kinds[str(metadata["kind"])] += 1
        components[str(metadata["scope"]["component"])] += 1
        if metadata["status"] != "published":
            add_issue(path, stable_id, "not_published", str(metadata["status"]))
        if metadata["owner"] is None:
            add_issue(path, stable_id, "owner_missing", "owner is null")
        if metadata["kind"] == "unclassified":
            add_issue(
                path, stable_id, "kind_unclassified", "knowledge kind is unresolved"
            )
        if metadata["scope"]["basis"] == "unresolved":
            add_issue(path, stable_id, "scope_unresolved", "scope basis is unresolved")
        if "unresolved" in metadata["scope"]["software_versions"]:
            add_issue(
                path, stable_id, "version_unresolved", "software version is unresolved"
            )
        for source in metadata["sources"]:
            if source["snapshot_digest"] is None:
                add_issue(
                    path,
                    stable_id,
                    "source_snapshot_missing",
                    f"source {source['id']} has no immutable snapshot digest",
                )
        passed = {
            (claim_id, validation["layer"])
            for validation in metadata["validation"]
            if validation["result"] == "passed"
            for claim_id in validation["claim_refs"]
        }
        for claim in metadata["claims"]:
            missing = [
                layer
                for layer in claim["required_validation"]
                if (claim["id"], layer) not in passed
            ]
            if missing:
                add_issue(
                    path,
                    stable_id,
                    "claim_validation_missing",
                    f"claim {claim['id']} lacks {','.join(missing)}",
                )
        if metadata["review"] is None:
            add_issue(path, stable_id, "review_missing", "review metadata is null")

    has_professional = _table_exists(conn, "professional_knowledge_revisions")
    has_source_changes = _table_exists(conn, "professional_source_change_events")
    has_projection_column = _column_exists(
        conn, "knowledge_entries", "professional_revision_id"
    )
    published_current = (
        int(
            conn.execute(
                """SELECT count(*) FROM knowledge_entries ke
                     JOIN professional_knowledge_revisions pkr
                       ON pkr.revision_id=ke.professional_revision_id
                    WHERE ke.status='approved' AND pkr.lifecycle_state='published'"""
            ).fetchone()[0]
        )
        if has_professional and has_projection_column
        else 0
    )
    needs_review = (
        int(
            conn.execute(
                """SELECT count(*) FROM professional_knowledge_revisions
                    WHERE lifecycle_state='needs_review'"""
            ).fetchone()[0]
        )
        if has_professional
        else 0
    )
    source_change_events = (
        int(
            conn.execute(
                "SELECT count(*) FROM professional_source_change_events"
            ).fetchone()[0]
        )
        if has_source_changes
        else 0
    )
    legacy_sql = (
        """SELECT count(*) FROM knowledge_entries
             WHERE status='approved' AND professional_revision_id IS NULL"""
        if has_projection_column
        else "SELECT count(*) FROM knowledge_entries WHERE status='approved'"
    )
    db_counts = {
        "published_current": published_current,
        "needs_review": needs_review,
        "source_change_events": source_change_events,
        "legacy_approved": int(conn.execute(legacy_sql).fetchone()[0]),
        "schema_supports_professional": has_professional and has_projection_column,
    }
    recent_changes = (
        [
            dict(row)
            for row in conn.execute(
                """SELECT pkr.stable_id,pkc.local_claim_id,e.repository,e.changed_path,
                          e.change_id,e.revision,e.created_at
                     FROM professional_source_change_events e
                     JOIN professional_knowledge_revisions pkr
                       ON pkr.revision_id=e.knowledge_revision_id
                     JOIN professional_knowledge_claims pkc ON pkc.claim_id=e.claim_id
                    ORDER BY e.created_at DESC LIMIT 20"""
            )
        ]
        if has_source_changes and has_professional
        else []
    )
    publish_ready = bool(paths) and total_issues == 0
    automatic_reply_blockers = []
    if not publish_ready:
        automatic_reply_blockers.append("repository_publish_gates_not_satisfied")
    automatic_reply_blockers.append("gold_evaluation_evidence_not_recorded")
    return {
        "generated_at": observed.isoformat(),
        "repository_root": str(root),
        "repository": {
            "article_count": len(paths),
            "valid_article_count": valid_articles,
            "states": dict(sorted(states.items())),
            "kinds": dict(sorted(kinds.items())),
            "components": dict(sorted(components.items())),
            "issue_count": total_issues,
            "issues": issues,
            "issues_truncated": total_issues > len(issues),
        },
        "database": db_counts,
        "recent_source_changes": recent_changes,
        "source_refresh": source_refresh_snapshot(conn, now=observed),
        "ready_for_publish": publish_ready,
        "ready_for_automatic_reply": False,
        "automatic_reply_blockers": automatic_reply_blockers,
    }


def render_professional_knowledge(snapshot: dict[str, Any]) -> str:
    repository = snapshot["repository"]
    database = snapshot["database"]
    lines = [
        "专业知识工作台",
        (
            f"仓库条目 {repository['article_count']}｜结构有效 {repository['valid_article_count']}｜"
            f"门禁问题 {repository['issue_count']}"
        ),
        (
            f"线上发布 {database['published_current']}｜待复核 {database['needs_review']}｜"
            f"遗留已审核 {database['legacy_approved']}｜源码变更事件 {database['source_change_events']}"
        ),
        (
            "发布就绪："
            + ("是" if snapshot["ready_for_publish"] else "否")
            + "｜自动回复就绪："
            + ("是" if snapshot["ready_for_automatic_reply"] else "否")
        ),
    ]
    refresh = snapshot.get("source_refresh", {})
    if refresh.get("available"):
        lines.append(
            f"来源刷新失败 {refresh['failed_sources']}｜等待重试 {refresh['retry_deferred']}｜"
            f"正在刷新 {refresh['in_flight']}"
        )
    if repository["issues"]:
        lines.extend(["", "阻塞项："])
        for issue in repository["issues"]:
            lines.append(
                f"- [{issue['code']}] {issue['id']} · {issue['detail']} · {issue['path']}"
            )
    if snapshot["automatic_reply_blockers"]:
        lines.extend(
            [
                "",
                "自动回复门禁：" + "、".join(snapshot["automatic_reply_blockers"]),
            ]
        )
    if snapshot["recent_source_changes"]:
        lines.extend(["", "最近源码失效："])
        for item in snapshot["recent_source_changes"]:
            lines.append(
                f"- {item['stable_id']}#{item['local_claim_id']} ← "
                f"{item['repository']}:{item['changed_path']} ({item['change_id']})"
            )
    return "\n".join(lines)
