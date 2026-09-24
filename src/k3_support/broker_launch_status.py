"""Bounded read-only projection; absence of records is not service health."""

from .broker_remote_state import UNSETTLED


def snapshot(conn):
    rows = conn.execute("SELECT l.*, EXISTS(SELECT 1 FROM broker_execution_instances i "
                        "JOIN broker_remote_actions a USING(grant_id) "
                        "LEFT JOIN broker_remote_results r ON r.request_id=a.request_id "
                        f"WHERE i.claim_request_id=l.claim_request_id AND {UNSETTLED}) AS remote_pending, "
                        "EXISTS(SELECT 1 FROM broker_execution_instances i WHERE i.claim_request_id=l.claim_request_id "
                        "AND NOT EXISTS(SELECT 1 FROM broker_execution_resources r WHERE r.grant_id=i.grant_id)) AS resource_binding_missing "
                        "FROM broker_launches l WHERE l.state!='finished' ORDER BY l.created_at,l.claim_request_id LIMIT 20").fetchall()
    labels = {"launching": "启动结果待核对", "accepted": "启动请求已接收，执行状态待核验",
              "unknown": "启动结果未知，不会自动重试"}
    return {"read_only": True, "unresolved": [
        {"claim_request_id": row["claim_request_id"], "state": row["state"],
         "resource_binding_missing": bool(row['resource_binding_missing']),
         "label": ("历史资源绑定尚未核验，保留占位；需核对原执行输入与板卡会话" if row['resource_binding_missing']
                   else "远端操作或退出记录尚待核对，暂不进入后续处理" if row["remote_pending"]
                   else labels.get(row["state"], "未知状态")), "updated_at": row["updated_at"]}
        for row in rows], "warning": "存在未核对启动记录时，调度不会新建启动请求。请保留编号和证据，不要删除记录强制重试。"}
