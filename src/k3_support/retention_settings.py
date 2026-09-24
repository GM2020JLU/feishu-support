"""Explicit, session-bound TTL policy changes; never clears bodies itself."""

from datetime import timedelta

from .db import transaction
from .ids import digest, new_id
from .timeutil import iso_now, parse_iso, utc_now


def validate(days):
    if days is not None and (type(days) is not int or not 1 <= days <= 3650):
        raise ValueError('正文保留期必须关闭或为 1–3650 天整数')
    return days


def _spec(scope):
    if scope == 'body':
        return 'body_retention', 'body_retention_days', 'database_payload_only_not_files_backups_or_secure_erasure'
    if scope == 'draft':
        return 'draft_retention', 'captured_draft_retention_days', 'unreviewed_captured_draft_only_not_files_or_backups'
    raise ValueError('unknown retention policy scope')


def snapshot(conn, config, *, scope='body'):
    table, key, description = _spec(scope)
    baseline = validate(config.raw['policy'].get(key))
    base = digest({key: baseline})
    row = conn.execute(f'SELECT * FROM {table}_settings WHERE singleton=1').fetchone()
    mismatch = bool(row and row['base_digest'] != base)
    return {'revision': row['revision'] if row else 0, 'base_digest': base,
            'days': None if mismatch else validate(row['days'] if row else baseline),
            'needs_migration': mismatch,
            'scope': description}


def preview(conn, config, *, days, expected_revision, session_id, scope='body'):
    table, _, _ = _spec(scope)
    validate(days)
    if not isinstance(session_id, str) or not session_id:
        raise ValueError('缺少登录会话')
    with transaction(conn):
        current = snapshot(conn, config, scope=scope)
        if type(expected_revision) is not int or expected_revision != current['revision']:
            raise ValueError('策略已变化，请重新读取')
        if current['needs_migration'] and days is not None:
            raise ValueError('基础配置已变化，请先明确关闭并应用，再重新设置保留期')
        identifier = new_id('brp')
        conn.execute(f'INSERT INTO {table}_policy_drafts VALUES(?,?,?,?,?,?,NULL)',
                     (identifier, session_id, current['base_digest'], current['revision'], days,
                      (utc_now() + timedelta(minutes=10)).isoformat()))
    return {'draft_id': identifier, 'previous_days': current['days'], 'days': days,
            'warning': '启用或缩短后，后台可能清理符合条件的' + ('旧正文' if scope == 'body' else '未审核草稿') + '；关闭或延长不能恢复已清理内容。此操作不删除文件或备份。'}


def apply(conn, config, *, draft_id, session_id, actor_id, scope='body'):
    table, _, _ = _spec(scope)
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError('缺少控制者身份')
    with transaction(conn):
        draft = conn.execute(f'SELECT * FROM {table}_policy_drafts WHERE draft_id=?', (draft_id,)).fetchone()
        if draft is None or draft['session_id'] != session_id:
            raise ValueError('预览不属于当前会话')
        if draft['applied_revision'] is not None:
            return {'revision': draft['applied_revision'], 'replayed': True}
        current = snapshot(conn, config, scope=scope)
        if (parse_iso(draft['expires_at']) <= utc_now() or draft['revision'] != current['revision']
                or draft['base_digest'] != current['base_digest']):
            raise ValueError('预览已失效，请重新读取和预览')
        revision = current['revision'] + 1
        now = iso_now()
        conn.execute(f'INSERT OR REPLACE INTO {table}_settings VALUES(1,?,?,?,?,?)',
                     (revision, current['base_digest'], draft['days'], now, actor_id))
        conn.execute(f'INSERT INTO {table}_policy_history VALUES(?,?,?,?,?)',
                     (revision, current['days'], draft['days'], actor_id, now))
        conn.execute(f'UPDATE {table}_policy_drafts SET applied_revision=? WHERE draft_id=?', (revision, draft_id))
    return {'revision': revision, 'days': draft['days'], 'replayed': False,
            'bodies_cleared_by_this_request': 0}
