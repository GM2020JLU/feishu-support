"""Bounded operator audit projection; excludes payloads, tokens and model prompts."""

import base64
import json
import math

KINDS = (
    "all",
    "mode",
    "features",
    "budget",
    "work_hours",
    "notifications",
    "knowledge",
    "knowledge_import",
    "profiles",
    "subscriptions",
    "execution",
    "case",
    "approval",
    "approval_history",
    "delivery",
)
_UNION = """
SELECT 'mode:'||event_id AS key,'mode' AS kind,created_at AS occurred_at,actor_id,
       scope AS target,coalesce(before_mode,'unset')||' → '||after_mode AS summary FROM global_control_events
UNION ALL SELECT 'features:'||revision,'features',updated_at,updated_by,'feature_settings','配置版本 '||revision FROM feature_settings_history
UNION ALL SELECT 'budget:'||revision,'budget',created_at,actor_id,'model_budget','预算版本 '||revision FROM model_budget_policy_history
UNION ALL SELECT 'work_hours:'||printf('%020d',revision),'work_hours',updated_at,actor_id,'work_hours','工作时间版本 '||revision FROM work_hours_history
UNION ALL SELECT 'notifications:'||request_id,'notifications',updated_at,updated_by,'notifications',
       '提醒版本 '||revision||' / 夜间汇总 '||night_enabled||' / 手动静音至 '||coalesce(until_at,'未设置') FROM notification_snooze_history
UNION ALL SELECT 'knowledge:'||request_id,'knowledge',created_at,actor_id,knowledge_id,previous_status||' → '||decision FROM knowledge_lifecycle_actions
UNION ALL SELECT 'knowledge_import:'||draft_id,'knowledge_import',applied_at,applied_by,draft_id,
       '专业知识包导入已提交；不代表自动回复已授权' FROM knowledge_import_drafts WHERE applied_at IS NOT NULL
UNION ALL SELECT 'profiles:'||request_id,'profiles',created_at,actor_id,requester_id,
       '本地角色修正已提交；不代表通讯录已更新' FROM profile_actions
UNION ALL SELECT 'subscriptions:'||request_id,'subscriptions',json_extract(result_json,'$.updated_at'),
       json_extract(result_json,'$.owner_id'),json_extract(result_json,'$.category'),
       '关注版本 '||json_extract(result_json,'$.revision')||' / '||
       CASE json_extract(result_json,'$.enabled') WHEN 1 THEN '已订阅' ELSE '已退订' END||
       ' / 稍后至 '||coalesce(json_extract(result_json,'$.snooze_until'),'未设置')||'（不代表已发送提醒）'
       FROM attention_subscription_history
UNION ALL SELECT 'watch_subscriptions:'||request_id,'subscriptions',json_extract(result_json,'$.updated_at'),
       json_extract(result_json,'$.owner_id'),json_extract(result_json,'$.source_kind')||':'||json_extract(result_json,'$.source_key'),
       '关注版本 '||json_extract(result_json,'$.revision')||' / '||
       CASE json_extract(result_json,'$.enabled') WHEN 1 THEN '已订阅' ELSE '已退订' END||
       '（本地动态关注，不代表已发送提醒）' FROM watch_subscription_history
UNION ALL SELECT 'execution:'||request_id,'execution',requested_at,actor_id,job_id,'已接受停止请求；退出及收尾另行核验' FROM execution_stop_requests
UNION ALL SELECT 'execution_recovery:'||request_id,'execution',requested_at,actor_id,job_id,'原输入重新校验后恢复排队；不代表执行或上板已获授权' FROM broker_recovery_actions
UNION ALL SELECT 'case:'||event_id,'case',created_at,coalesce(actor_id,actor_type),case_id,
       event_type||' / '||coalesce(before_state,'未记录')||' → '||coalesce(after_state,'未记录') FROM case_events
UNION ALL SELECT 'approval:'||approval_id,'approval',updated_at,'状态更新人未记录',approval_id,
       approval_type||' / 当前状态 '||status||'（当前快照，非逐次动作历史）' FROM approvals
UNION ALL SELECT 'approval_history:'||printf('%020d',sequence),'approval_history',observed_at,coalesce(decision_actor,'操作者未记录'),approval_id,
       event_kind||' / '||coalesce(before_status,'无')||' → '||coalesce(after_status,'无') FROM approval_audit_events
UNION ALL SELECT 'delivery_current:'||outbox_id,'delivery',updated_at,'状态更新人未记录',outbox_id,
       channel||' / 当前发送状态 '||state||'（快照；不代表对方已读）' FROM outbox
UNION ALL SELECT 'delivery_claim:'||outbox_id||':'||attempt_number,'delivery',claimed_at,worker_id,outbox_id,
       '尝试 '||attempt_number||' / 已领取，尚不证明派发' FROM outbox_attempts
UNION ALL SELECT 'delivery_dispatch:'||outbox_id||':'||attempt_number,'delivery',dispatch_started_at,worker_id,outbox_id,
       '尝试 '||attempt_number||' / 已登记开始派发，不证明成功' FROM outbox_attempts WHERE dispatch_started_at IS NOT NULL
UNION ALL SELECT 'delivery_event:'||e.event_id,'delivery',e.recorded_at,'结果记录者未记录',a.outbox_id,
       '尝试 '||a.attempt_number||' / 结果事件 '||e.event_type||'（不代表对方已读）'
       FROM outbox_attempt_events e JOIN outbox_attempts a USING(claim_token)
"""


def page(conn, *, kind="all", cursor=None, limit=30):
    if kind not in KINDS or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("invalid audit filter")
    after = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 1024:
                raise ValueError()
            after = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if (
                not isinstance(after, list)
                or len(after) != 3
                or after[0] != kind
                or type(after[1]) not in (float, int)
                or not math.isfinite(after[1])
                or not isinstance(after[2], str)
                or len(after[2]) > 200
            ):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise ValueError(
                "invalid audit cursor; reload the selected category"
            ) from None
    cte = (
        "WITH records AS ("
        + _UNION
        + "), ordered AS (SELECT *,coalesce(julianday(occurred_at),-1.0) AS stamp FROM records) "
    )
    where = "(?='all' OR kind=?)"
    params = [kind, kind]
    conn.execute("SAVEPOINT audit_inventory")
    try:
        total = conn.execute(
            cte + "SELECT count(*) FROM ordered WHERE " + where, params
        ).fetchone()[0]
        if after:
            where += " AND (stamp<? OR (stamp=? AND key<?))"
            params.extend([after[1], after[1], after[2]])
        rows = conn.execute(
            cte
            + "SELECT * FROM ordered WHERE "
            + where
            + " ORDER BY stamp DESC,key DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        items = [
            {
                key: row[key]
                for key in (
                    "key",
                    "kind",
                    "occurred_at",
                    "actor_id",
                    "target",
                    "summary",
                )
            }
            for row in rows[:limit]
        ]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([kind, last["stamp"], last["key"]]).encode()
            ).decode()
        return {
            "items": items,
            "total_matching": total,
            "next_cursor": next_cursor,
            "read_only": True,
            "live": True,
            "scope": list(KINDS[1:]),
        }
    finally:
        conn.execute("RELEASE audit_inventory")
