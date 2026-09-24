"""Read-only retention metadata; deliberately excludes paths and message bodies."""

LABELS = {"prepared": "等待隔离或核对", "quarantined": "已隔离，未永久删除",
          "restored": "已恢复", "cancelled": "已取消", "failed": "处理失败，需核对"}


def page(conn, *, state="all", after_id="", limit=30):
    if (not isinstance(state, str) or state not in {"all", *LABELS}
            or not isinstance(after_id, str) or len(after_id) > 100
            or type(limit) is not int or not 1 <= limit <= 50):
        raise ValueError("invalid retention inventory filter")
    conn.execute("SAVEPOINT retention_inventory")
    try:
        total = conn.execute("SELECT count(*) FROM retention_attempts WHERE (?='all' OR state=?)", (state, state)).fetchone()[0]
        rows = conn.execute("SELECT attempt_id,event_pk,state,received_at,updated_at, "
                            "(SELECT request_id FROM retention_recovery_requests r WHERE r.attempt_id=retention_attempts.attempt_id ORDER BY created_at DESC,request_id DESC LIMIT 1) AS recovery_request_id, "
                            "(SELECT actor_id FROM retention_recovery_requests r WHERE r.attempt_id=retention_attempts.attempt_id ORDER BY created_at DESC,request_id DESC LIMIT 1) AS recovery_actor, "
                            "(SELECT state FROM retention_recovery_requests r WHERE r.attempt_id=retention_attempts.attempt_id ORDER BY created_at DESC,request_id DESC LIMIT 1) AS recovery_state, "
                            "(SELECT request_id FROM retention_purge_requests p WHERE p.attempt_id=retention_attempts.attempt_id ORDER BY created_at DESC,request_id DESC LIMIT 1) AS purge_request_id, "
                            "(SELECT state FROM retention_purge_requests p WHERE p.attempt_id=retention_attempts.attempt_id ORDER BY created_at DESC,request_id DESC LIMIT 1) AS purge_state FROM retention_attempts "
                            "WHERE (?='all' OR state=?) AND attempt_id>? ORDER BY attempt_id LIMIT ?",
                            (state, state, after_id, limit+1)).fetchall()
        return {"items": [{**dict(row), "label": LABELS[row["state"]]} for row in rows[:limit]],
                "total_matching": total, "next_cursor": rows[limit-1]["attempt_id"] if len(rows) > limit else None,
                "read_only": True,
                "note": "仅显示数据库记录，不检查文件现状；浏览不会清理、恢复或永久删除资料。"}
    finally:
        conn.execute("RELEASE retention_inventory")
