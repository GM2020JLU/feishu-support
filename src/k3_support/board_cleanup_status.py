"""Owner-facing cleanup metadata, without device I/O or automatic notifications."""

LABELS = {
    "running": "收尾已启动，尚未确认完成",
    "unknown": "收尾结果待核对，不会自动复位重试",
    "succeeded": "本次 BROM 收尾记录已完成",
}


def pending(conn):
    """Global attention remains visible independently of the job-list filter."""
    count = conn.execute("SELECT count(*) FROM broker_board_cleanup WHERE state IN ('running','unknown')").fetchone()[0]
    rows = conn.execute(
        "SELECT b.grant_id,b.session_id,b.state,b.updated_at,j.case_id "
        "FROM broker_board_cleanup b JOIN broker_grants g USING(grant_id) JOIN jobs j USING(job_id) "
        "WHERE b.state IN ('running','unknown') ORDER BY b.created_at,b.grant_id LIMIT 5")
    return {"total": count, "items": [{**dict(row), "label": LABELS[row["state"]]} for row in rows]}


def items(conn, *, job_id=None, case_id=None):
    if (job_id is None) == (case_id is None):
        raise ValueError("one cleanup scope required")
    field, value = ("j.job_id", job_id) if job_id is not None else ("j.case_id", case_id)
    return [{**dict(row), "label": LABELS.get(row["state"], "收尾状态未知")}
            for row in conn.execute(
                "SELECT b.grant_id,b.session_id,b.state,b.updated_at,g.lifecycle_round,g.attempt_no "
                "FROM broker_board_cleanup b JOIN broker_grants g USING(grant_id) JOIN jobs j USING(job_id) "
                f"WHERE {field}=? ORDER BY b.created_at DESC,b.grant_id DESC LIMIT 5", (value,))]


def lines(conn, *, case_id):
    rows = items(conn, case_id=case_id)
    if not rows:
        return []
    result = ["", "板卡收尾（最近 5 次，含历史轮次）",
              "完成记录不代表板卡当前空闲，也不代表故障已修复。"]
    for row in rows:
        result.append(f"第 {row['lifecycle_round']} 轮 / 第 {row['attempt_no']} 次执行 · {row['label']}\n"
                      f"会话 {row['session_id']} · 更新 {row['updated_at']}")
    return result
