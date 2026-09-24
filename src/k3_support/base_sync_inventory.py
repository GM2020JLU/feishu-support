"""Bounded read-only Base recovery inventory; never proof of remote completion."""


def snapshot(conn, *, limit=50, after_operation='', after_legacy_job=''):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('Base recovery limit must be between 1 and 200')
    if any(not isinstance(value, str) or len(value) > 200 for value in (after_operation, after_legacy_job)):
        raise ValueError('invalid Base recovery cursor')
    conn.execute('SAVEPOINT base_recovery_inventory')
    try:
        operations = conn.execute('''SELECT operation_id,base_digest,table_id,entity_type,entity_id,
            job_id,attempt_no,state,target_version,request_digest,write_digest,record_id,created_at,updated_at
            FROM base_sync_operations WHERE state IN ('reserved','prepared','dispatched','unknown')
            AND operation_id>? ORDER BY operation_id LIMIT ?''', (after_operation, limit+1)).fetchall()
        legacy = conn.execute('''SELECT job_id,entity_type,entity_id,state FROM base_sync_legacy_holds
            WHERE state='unverified' AND job_id>? ORDER BY job_id LIMIT ?''', (after_legacy_job, limit+1)).fetchall()
        return {
            'read_only': True, 'remote_completion_verified': False,
            'operations': [dict(row) for row in operations[:limit]],
            'legacy_holds': [dict(row) for row in legacy[:limit]],
            'next_operation': operations[limit-1]['operation_id'] if len(operations) > limit else None,
            'next_legacy_job': legacy[limit-1]['job_id'] if len(legacy) > limit else None,
            'guidance': '已发出或结果未知的请求不会因租约到期而重试。先核对远端请求确已结束及实际记录 ID；'
                        '看到相同字段、进程退出或超时均不能证明远端请求不会稍后生效。历史执行须在停止旧同步进程后盘点。'
                        '不要删除占位或改数据库状态强制重试。',
        }
    finally:
        conn.execute('RELEASE base_recovery_inventory')
