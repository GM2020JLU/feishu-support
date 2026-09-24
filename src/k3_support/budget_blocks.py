"""Deduplicated local diagnostics, no prompts, credentials, or push notifications."""

import sqlite3

from .ids import digest
from .timeutil import iso_now

REASONS = {
    "identity_unverified": "核对本机 Hermes 清单及模型身份；未派发模型调用。",
    "budget_gate_blocked": "核对额度、同一请求及未知费用记录；不要自动重试或清空占用。",
    "ledger_unavailable": "核对账本连接及权限；只读评估不能绕过调用前预留。",
    "model_result_unconfirmed": "模型结果未完整确认；已保留预留占用，核对调用记录，不自动重试。",
}


def record(conn, scope, function, reason):
    if reason not in REASONS:
        raise ValueError("unknown budget block reason")
    now = iso_now()
    try:
        conn.execute(
            "INSERT INTO model_budget_blocks VALUES(?,?,?,1,?,?,NULL) "
            "ON CONFLICT(scope_digest,function_name,reason) DO UPDATE SET "
            "occurrences=occurrences+1,last_seen_at=excluded.last_seen_at,resolved_at=NULL",
            (digest(scope), function, reason, now, now),
        )
    except sqlite3.Error:
        # A read-only or broken ledger is not permission to open another writer.
        return False
    return True


def resolve(conn, scope, function):
    try:
        conn.execute(
            "UPDATE model_budget_blocks SET resolved_at=? WHERE scope_digest=? "
            "AND function_name=? AND resolved_at IS NULL",
            (iso_now(), digest(scope), function),
        )
    except sqlite3.Error:
        return False
    return True


def report(conn):
    rows = conn.execute(
        "SELECT * FROM model_budget_blocks WHERE resolved_at IS NULL "
        "ORDER BY last_seen_at DESC,scope_digest,function_name,reason LIMIT 50"
    ).fetchall()
    return {
        "items": [{**dict(row), "next_step": REASONS[row["reason"]]} for row in rows],
        "total_open": conn.execute(
            "SELECT count(*) FROM model_budget_blocks WHERE resolved_at IS NULL"
        ).fetchone()[0],
        "notification_sent": False,
        "retention": "deduplicated_history",
    }
