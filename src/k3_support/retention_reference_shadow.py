"""Resumable independent source-table traversal, not index-row enumeration."""

import hashlib
import json

from .db import transaction
from .retention_transaction import bounded_transaction
from .retention_reference_oracle import OracleIncomplete, json_reference_digests
from .retention_reference_sources import identifier


def scan_source(conn, source_id, *, limit=10):
    if type(limit) is not int or not 1 <= limit <= 30:
        raise ValueError('shadow batch limit must be between 1 and 30')
    with transaction(conn, immediate=False):
        source = conn.execute('SELECT * FROM retention_reference_sources WHERE source_id=?', (source_id,)).fetchone()
        if source is None or not source['backfill_complete']:
            raise ValueError('source backfill is incomplete')
        if source['shadow_complete']:
            return {'scanned': 0, 'complete': True}
        keys = json.loads(source['key_spec'])
        columns = ','.join(identifier(key) for key in keys)
        cursor = json.loads(source['shadow_cursor']) if source['shadow_cursor'] else None
        where, params = '', []
        if cursor is not None:
            left = f'({columns})' if len(keys) > 1 else columns
            right = '('+','.join('?' for _ in keys)+')' if len(keys) > 1 else '?'
            where, params = f' WHERE {left}>{right}', cursor
        column = identifier(source['column_name'])
        size = f'coalesce(length(CAST({column} AS BLOB)),0)'
        records = conn.execute(f'SELECT json_array({columns}),{size},'
            f'CASE WHEN {size}<=1048576 THEN {column} END FROM {identifier(source["table_name"])}'
            f'{where} ORDER BY {columns} LIMIT ?', (*params, limit+1)).fetchall()
        snapshots = []
        for row in records[:limit]:
            indexed = conn.execute('''SELECT revision,deleted,state FROM retention_reference_rows
                WHERE source_id=? AND row_key=?''', (source_id, row[0])).fetchone()
            snapshots.append((tuple(row), tuple(indexed) if indexed else None))
    outcomes = []
    kinds = set(json.loads(source['kinds']))
    for (key, size, value), indexed in snapshots:
        error, expected = None, set()
        if indexed is not None and indexed[1:] == (0, 'pending'):
            # A captured write may precede dirty consumption. It is not proof
            # of missing coverage or corruption; retain the cursor and retry.
            return {'scanned': 0, 'complete': False, 'pending': True}
        if indexed is None or indexed[1:] != (0, 'complete'):
            error = 'missing_or_incomplete_index_row'
        elif size > 1048576:
            error = 'shadow_value_budget_exceeded'
        else:
            try:
                if 'json' in kinds:
                    expected.update(json_reference_digests(value))
                if kinds - {'json', 'inbound_fk', 'external_id'}:
                    raise OracleIncomplete('unknown source kind')
                if kinds & {'inbound_fk', 'external_id'} and value is not None:
                    if not isinstance(value, str):
                        raise OracleIncomplete('non-text identity')
                    expected.add(hashlib.sha256(value.encode('utf-8')).hexdigest())
            except (OracleIncomplete, UnicodeError):
                error = 'shadow_extraction_incomplete'
        outcomes.append((key, indexed, expected, error))
    with bounded_transaction(conn):
        current_source = conn.execute('SELECT shadow_cursor FROM retention_reference_sources WHERE source_id=?', (source_id,)).fetchone()
        if current_source is None or current_source[0] != source['shadow_cursor']:
            return {'scanned': 0, 'complete': False, 'stale': True}
        for key, indexed, expected, error in outcomes:
            current = conn.execute('SELECT revision,deleted,state FROM retention_reference_rows WHERE source_id=? AND row_key=?', (source_id, key)).fetchone()
            if (tuple(current) if current else None) != indexed:
                return {'scanned': 0, 'complete': False, 'stale': True}
            actual = {row[0] for row in conn.execute('SELECT target_digest FROM retention_reference_edges WHERE source_id=? AND row_key=?', (source_id, key))}
            if error is None and actual != expected:
                error = 'shadow_edge_mismatch'
            if error:
                conn.execute('UPDATE retention_reference_sources SET shadow_error=? WHERE source_id=?', (error, source_id))
                return {'scanned': 0, 'complete': False, 'error': error}
        for key, indexed, _, _ in outcomes:
            conn.execute('UPDATE retention_reference_rows SET shadow_revision=? WHERE source_id=? AND row_key=?', (indexed[0], source_id, key))
        complete = len(records) <= limit
        checkpoint = outcomes[-1][0] if outcomes else source['shadow_cursor']
        conn.execute('UPDATE retention_reference_sources SET shadow_cursor=?,shadow_complete=?,shadow_error=NULL WHERE source_id=?', (checkpoint, int(complete), source_id))
        return {'scanned': len(outcomes), 'complete': complete}
