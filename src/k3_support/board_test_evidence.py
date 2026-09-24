"""Scoped historical board receipts, never inferred environment or resolution."""
from .ids import digest
from .board_serial_evidence import boot_markers
from .board_serial_evidence import version_observations
from .board_serial_evidence import boot_attempt_observations
import json


def reviewed_versions(conn, *, case_id, lifecycle_round):
    return reviewed_version_scan(conn, case_id=case_id, lifecycle_round=lifecycle_round)['observations']


def reviewed_version_scan(conn, *, case_id, lifecycle_round):
    conn.execute('SAVEPOINT reviewed_version_snapshot')
    try:
        return _reviewed_version_scan(conn, case_id=case_id, lifecycle_round=lifecycle_round)
    finally:
        conn.execute('RELEASE reviewed_version_snapshot')


def _reviewed_version_scan(conn, *, case_id, lifecycle_round):
    from .content_retirement import require_case_content
    require_case_content(conn, case_id=case_id, lifecycle_round=lifecycle_round)
    from .review_evidence import verified_board_output
    observations = []
    boot_attempts = []
    reviews = conn.execute('''SELECT r.review_id,r.case_id,r.job_id,
        CASE WHEN length(CAST(r.evidence_ids_json AS BLOB))<=262144
             THEN r.evidence_ids_json END AS evidence_ids_json,
        CASE WHEN length(CAST(r.independent_checks_json AS BLOB))<=262144
             THEN r.independent_checks_json END AS independent_checks_json
        FROM codex_reviews r JOIN jobs j ON j.job_id=r.job_id
        WHERE r.case_id=? AND j.lifecycle_round=? ORDER BY r.rowid DESC LIMIT 11''',
        (case_id, lifecycle_round)).fetchall()
    truncated = len(reviews) > 10
    unreadable = False
    seen = set()
    for review in reviews[:10]:
        if review['independent_checks_json'] is None:
            unreadable = True
            continue
        try:
            ids = json.loads(review['evidence_ids_json'])
            if not isinstance(ids, list):
                unreadable = True
                continue
        except (ValueError, TypeError):
            unreadable = True
            continue
        truncated = truncated or len(ids) > 100
        for evidence_id in ids[:100]:
            if not isinstance(evidence_id, str) or evidence_id in seen:
                continue
            item = conn.execute('SELECT * FROM evidence WHERE evidence_id=? AND case_id=?',
                                (evidence_id, case_id)).fetchone()
            if item is None:
                continue
            receipt = verified_board_output(conn, review=review, item=item)
            if receipt is None or receipt['action'].get('type') != 'serial_wait':
                continue
            seen.add(evidence_id)
            for version in version_observations(receipt['stdout']):
                observations.append({**version, 'observed_at': receipt['observed_at'],
                                     'session_id': receipt['session_id'], 'evidence_id': evidence_id})
            for attempt in boot_attempt_observations(receipt['stdout']):
                boot_attempts.append({**attempt, 'observed_at': receipt['observed_at'],
                                      'session_id': receipt['session_id'], 'evidence_id': evidence_id})
    return {'observations': observations, 'truncated': truncated,
            'boot_attempts': boot_attempts,
            'unreadable_review_index': unreadable, 'complete_history_verified': False}

ACTION_LABELS = {"list": "板卡查询", "reset": "复位", "enter_brom": "进入下载模式",
                 "ram_boot": "RAM 启动", "serial_wait": "串口匹配", "serial_exec": "串口命令"}
STATE_LABELS = {"queued": "尚未执行", "running": "执行结果未定", "unknown": "结果未知，须核对",
                "cancelled": "已取消", "succeeded": "命令正常退出", "failed": "命令失败"}


def items(conn, *, case_id, lifecycle_round):
    conn.execute('SAVEPOINT board_items_snapshot')
    try:
        return _items(conn, case_id=case_id, lifecycle_round=lifecycle_round)
    finally:
        conn.execute('RELEASE board_items_snapshot')


def _items(conn, *, case_id, lifecycle_round):
    from .content_retirement import require_case_content
    require_case_content(conn, case_id=case_id, lifecycle_round=lifecycle_round)
    rows = conn.execute(
        """SELECT a.request_id,a.session_id,a.state,a.request_digest,
        json_extract(a.action_json,'$.type') AS action_type,g.attempt_no,
        r.exit_code,r.received_at,r.request_id AS result_id,
        CASE WHEN length(r.stdout)<=131072 THEN r.stdout END AS output,
        CASE WHEN length(r.stderr)<=131072 THEN r.stderr END AS error
        FROM broker_board_actions a JOIN broker_grants g USING(grant_id)
        JOIN jobs j ON j.job_id=g.job_id JOIN cases c ON c.case_id=j.case_id
        LEFT JOIN broker_board_results r ON r.request_id=a.request_id
        WHERE c.case_id=? AND c.lifecycle_round=? AND g.lifecycle_round=c.lifecycle_round
        ORDER BY a.created_at DESC,a.request_id DESC LIMIT 5""", (case_id, lifecycle_round))
    result = []
    for row in rows:
        state = row["state"]
        receipt = row["result_id"] is not None
        consistent = (
            state == "succeeded" and receipt and row["exit_code"] == 0
            or state == "failed" and receipt and row["exit_code"] > 0 and row["exit_code"] not in {124, 125, 255}
            or state in {"queued", "running", "cancelled"} and not receipt
            or state == "unknown"
        )
        if row["action_type"] not in ACTION_LABELS or receipt and (row["output"] is None or row["error"] is None):
            consistent = False
        result.append({"request_id": row["request_id"], "session_id": row["session_id"],
                       "attempt_no": row["attempt_no"], "action": ACTION_LABELS.get(row["action_type"], "未知操作"),
                       "label": STATE_LABELS[state] if consistent else "记录不一致，须核对",
                       "received_at": row["received_at"], "receipt_digest": digest(dict(row)),
                       "boot_markers": boot_markers(row['output']) if consistent and state == 'succeeded'
                       and row['action_type'] == 'serial_wait' else [],
                       "repair_verified": False, "environment_verified": False})
    return result


def lines(conn, *, case_id, lifecycle_round):
    conn.execute('SAVEPOINT board_lines_snapshot')
    try:
        return _lines(conn, case_id=case_id, lifecycle_round=lifecycle_round)
    finally:
        conn.execute('RELEASE board_lines_snapshot')


def _lines(conn, *, case_id, lifecycle_round):
    rows = items(conn, case_id=case_id, lifecycle_round=lifecycle_round)
    scan = reviewed_version_scan(conn, case_id=case_id, lifecycle_round=lifecycle_round)
    versions = scan['observations']
    attempts = scan.get('boot_attempts', [])
    if not rows and not versions and not attempts and not scan['truncated'] and not scan['unreadable_review_index']:
        return []
    output = ["", "本轮板卡操作回执（最近 5 条）",
              "与上方环境陈述分开：命令成功不等于启动正常、环境一致或现场故障已修复。",
              "此处仅展示回执摘要；不读取设备、不展示原始命令和日志。"]
    if scan['truncated']:
        output.append('版本证据达到读取上限（最近 10 次审核、每次最多 100 条证据），不是完整历史。')
    if scan['unreadable_review_index']:
        output.append('部分审核的证据索引无法读取，不能据此判断没有版本证据。')
    for row in rows:
        output.extend([
            f"第 {row['attempt_no']} 次执行 · {row['action']} · {row['label']}",
            f"会话 {row['session_id']} · 回执时间 {row['received_at'] or '尚无回执'}",
            f"请求 {row['request_id']} · 回执摘要 {row['receipt_digest']}",
        ])
        for marker in row['boot_markers']:
            output.append(f"历史串口标识：{marker['marker']}（不证明当前启动状态）；"
                          f"字符位置 {marker['start']}–{marker['end']} · 摘要 {marker['text_digest']}")
    if versions:
        output.append('审核证据中的组件版本（历史观测，不代表当前环境或现场已修复）')
        for item in versions:
            conflict = ' · 同一输出有多个版本，不能判定当前版本' if item['multiple_versions'] else ''
            output.append(f"{item['component']}：{item['version']} · {item['observed_at']} · "
                          f"会话 {item['session_id']} · 证据 {item['evidence_id']}{conflict}")
    if attempts:
        output.append('历史加载器尝试（不是成功启动介质，也不是根文件系统所在介质）')
        for item in attempts:
            output.append(f"尝试 {item['loader_label']} · {item['observed_at']} · "
                          f"会话 {item['session_id']} · 证据 {item['evidence_id']}")
        if any(item['truncated'] for item in attempts):
            output.append('单份串口记录最多展示 20 次加载器尝试，不是完整启动顺序。')
    return output
