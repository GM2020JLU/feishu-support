"""Explicit, transactional expiry of unreferenced captured draft payloads."""
import json
import re
from datetime import UTC, datetime, timedelta

from .body_retention import _identifier, _json_dependencies
from .db import transaction
from .ids import canonical_json, digest, new_id
from .timeutil import parse_iso


def preview(conn, *, candidate_id, days, now=None):
    if not isinstance(candidate_id, str) or not re.fullmatch(r'kcd_[0-9a-f]{64}', candidate_id):
        raise ValueError('invalid captured draft ID')
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('draft retention days must be 1..3650')
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        raise ValueError('aware retention clock required')
    row = conn.execute('SELECT * FROM knowledge_authoring_drafts WHERE candidate_id=?', (candidate_id,)).fetchone()
    if row is None:
        raise ValueError('captured draft not found')
    if any(not isinstance(row[key], str) for key in
           ('metadata_json', 'material_json', 'markdown', 'saved_at', 'revision_digest')):
        raise ValueError('captured draft has invalid text fields')
    metadata = json.loads(row['metadata_json'])
    if (not isinstance(metadata, dict) or not isinstance(metadata.get('id'), str)
            or not metadata['id'] or len(metadata['id']) > 256
            or not re.fullmatch(r'[0-9a-f]{64}', row['revision_digest'])):
        raise ValueError('captured draft identity is invalid')
    saved = parse_iso(row['saved_at'])
    blockers = []
    if saved.tzinfo is None or saved >= clock - timedelta(days=days):
        blockers.append('not_expired')
    if metadata.get('status') != 'captured' or metadata.get('review') is not None:
        blockers.append('not_unreviewed_capture')
    if conn.execute('SELECT 1 FROM professional_knowledge_revisions WHERE stable_id=? OR revision_digest=?',
                    (metadata.get('id'), row['revision_digest'])).fetchone():
        blockers.append('professional_revision_exists')
    columns = []
    for table in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if table[0] == 'retention_tombstones':
            continue
        columns.extend((table[0], column[1]) for column in conn.execute(f'PRAGMA table_info({_identifier(table[0])})')
                       if column[1].endswith('_json'))
        for fk in conn.execute(f'PRAGMA foreign_key_list({_identifier(table[0])})'):
            if fk[2] == 'knowledge_authoring_drafts' and conn.execute(
                f'SELECT 1 FROM {_identifier(table[0])} WHERE {_identifier(fk[3])}=? LIMIT 1',
                (row[fk[4]],),
            ).fetchone():
                blockers.append('foreign_key_reference')
    aliases = [row['revision_digest'], metadata['id']]
    dependencies, issues = _json_dependencies(conn, columns,
        [{'event_pk': candidate_id, 'external_id': alias} for alias in aliases])
    if dependencies[candidate_id] or issues:
        blockers.append('referenced_or_scan_incomplete')
    # Unified JSON scanning includes other drafts' metadata/material and ignores
    # only this row's own ID references. Nested values and map keys both count.
    return {'candidate_id': candidate_id, 'days': days, 'row_digest': digest({'row': dict(row), 'days': days}),
            'eligible': not blockers, 'blockers': sorted(set(blockers)),
            'scan_complete': not issues,
            'logical_bytes': sum(len(row[key].encode()) for key in ('markdown', 'metadata_json', 'material_json')),
            'read_only': True}


def clear(conn, *, candidate_id, days, expected_digest, actor_id, now=None, guard=None):
    if not isinstance(actor_id, str) or not 1 <= len(actor_id.strip()) <= 256:
        raise ValueError('explicit actor required')
    clock = now or datetime.now(UTC)
    with transaction(conn):
        if guard is not None and not guard():
            raise ValueError('draft retention policy changed or paused')
        current = preview(conn, candidate_id=candidate_id, days=days, now=clock)
        if not current['eligible'] or current['row_digest'] != expected_digest:
            raise ValueError('draft changed, retained or referenced; preview again')
        receipt = new_id('tmb')
        conn.execute("""INSERT INTO retention_tombstones
            (tombstone_id,artifact_type,artifact_path,artifact_hash,bytes_removed,status,reason,created_at)
            VALUES(?,'knowledge_draft',?,?,?,'deleted',?,?)""",
            (receipt, candidate_id, expected_digest, current['logical_bytes'],
             canonical_json({'actor_id': actor_id, 'days': days, 'logical_delete_only': True}), clock.isoformat()))
        conn.execute('DELETE FROM knowledge_authoring_drafts WHERE candidate_id=?', (candidate_id,))
    return {'receipt_id': receipt, 'candidate_id': candidate_id, 'logical_delete_only': True,
            'secure_erasure': False, 'backup_copies_removed': False}
