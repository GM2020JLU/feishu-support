"""Read-only Case accounting display; amounts are not provider invoices."""

from decimal import Decimal


def lines(conn, *, case_id):
    totals = conn.execute("SELECT currency,sum(charged) AS charged FROM model_budget_attempts "
                          "WHERE case_id=? GROUP BY currency ORDER BY currency", (case_id,)).fetchall()
    if not totals:
        return []
    output = ["", "模型预算（只读）", "占用额不是实际账单；未知费用保留占用，不按零处理。"]
    for row in totals:
        output.append(f"累计占用：{Decimal(row['charged']) / 1000000:f} {row['currency']}")
    labels = {"reserved": "已预留", "dispatched": "已授权启动，费用待确认",
              "unknown": "费用未知，保留占用", "settled": "已有费用回执", "cancelled": "启动前取消"}
    for row in conn.execute("SELECT a.state,a.charged,a.currency,b.grant_id,j.job_id,g.attempt_no "
                            "FROM model_budget_attempts a JOIN broker_budget_attempts b USING(attempt_id) "
                            "JOIN broker_grants g USING(grant_id) JOIN jobs j ON j.job_id=g.job_id "
                            "WHERE a.case_id=? AND j.case_id=a.case_id "
                            "ORDER BY a.created_at DESC,a.attempt_id DESC LIMIT 10", (case_id,)):
        output.append(f"独立编码任务 {row['job_id']} · 第 {row['attempt_no']} 次："
                      f"{labels.get(row['state'], '状态未知')} · 占用 {Decimal(row['charged']) / 1000000:f} {row['currency']}")
    return output
