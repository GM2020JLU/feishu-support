"""Operator-only metadata summary; no commands, tokens or remote output bodies."""

from .broker_remote_state import UNSETTLED


def lines(conn, *, case_id):
    relation = (" FROM broker_remote_actions a JOIN broker_grants g USING(grant_id) "
                "JOIN jobs j ON j.job_id=g.job_id LEFT JOIN broker_remote_results r ON r.request_id=a.request_id "
                "WHERE j.case_id=?")
    count = conn.execute("SELECT count(*),coalesce(sum("+UNSETTLED+"),0)"+relation, (case_id,)).fetchone()
    if not count[0]:
        return []
    output = ["", "远端执行（只读，含历史轮次）", f"共 {count[0]} 次操作 · {count[1]} 次执行或退出记录尚待核对",
              "以下最多显示最近 10 次；退出码 0 不代表故障修复，历史操作也不代表本轮已验证。"]
    if count[1]:
        output.append("待核对操作不会自动重跑；保留请求编号，不能靠删除记录恢复调度。")
    labels = {"queued": "等待执行", "running": "执行中", "unknown": "结果未知",
              "succeeded": "命令退出成功", "failed": "命令退出失败", "cancelled": "已取消"}
    rows = conn.execute("SELECT a.request_id,a.state,a.updated_at,g.lifecycle_round,g.attempt_no,r.exit_code,"+
                        UNSETTLED+" AS unsettled, (SELECT o.state FROM broker_remote_observations o "
                        "WHERE o.request_id=a.request_id ORDER BY o.created_at DESC,o.observation_id DESC LIMIT 1) AS observation_state"+
                        relation+" ORDER BY a.created_at DESC,a.request_id DESC LIMIT 10", (case_id,))
    for row in rows:
        label = labels.get(row["state"], "状态未知")
        if row["unsettled"] and row["state"] in ("succeeded", "failed", "queued", "cancelled"):
            label = "状态与退出记录不一致，待核对"
        if not row["unsettled"] and row["state"] == "unknown":
            label = "远端占用已解除；命令结果仍未知"
        code = "暂无回执" if row["exit_code"] is None else str(row["exit_code"])
        output.append(f"请求 {row['request_id']} · 第 {row['lifecycle_round']} 轮 / 第 {row['attempt_no']} 次执行\n"
                      f"{label} · 退出码 {code} · 更新 {row['updated_at']}")
        if row["observation_state"]:
            observed = {"pending": "查询尚无回执，不会自动重查", "observed": "已读到看护进程退出记录",
                        "unknown": "未取得可用回执", "stale": "查询期间状态变化，未采纳"}
            output.append("最近核对：" + observed.get(row["observation_state"], "未知") + "；不是恢复执行许可。")
    return output
