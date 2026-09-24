"""Bounded dirty-index consumption; no readiness or deletion authorization."""

import hashlib
import json

from .db import transaction
from .retention_transaction import bounded_transaction
from .ids import digest
from .retention_reference_extract import ExtractionIncomplete, extract_json, reference_digest
from .retention_reference_sources import identifier
from .retention_reference_oracle import OracleIncomplete, json_reference_digests


MAX_VALUE_BYTES = 1024 * 1024
MAX_BATCH_BYTES = 4 * 1024 * 1024


def consume_dirty(conn, *, limit=30):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('dirty batch limit must be between 1 and 100')
    # A read snapshot binds source contents to the observed capture revision.
    # Never parse payloads while holding the SQLite writer reservation.
    snapshots = []
    snapshot_bytes = 0
    with transaction(conn, immediate=False):
        records = conn.execute('''SELECT d.source_id,d.row_key,d.revision,r.deleted,
            s.table_name,s.column_name,s.key_spec,s.kinds
            FROM retention_reference_dirty d
            JOIN retention_reference_rows r USING(source_id,row_key)
            JOIN retention_reference_sources s USING(source_id)
            WHERE r.revision=d.revision ORDER BY d.source_id,d.row_key LIMIT ?''', (limit,)).fetchall()
        for record in records:
            item = dict(record)
            item['value'] = None
            item['read_error'] = None
            if not item['deleted']:
                keys, values = json.loads(item['key_spec']), json.loads(item['row_key'])
                if len(keys) != len(values) or any(value is None for value in values):
                    item['read_error'] = 'invalid_source_key'
                else:
                    where = ' AND '.join(f'{identifier(key)} IS ?' for key in keys)
                    column = identifier(item['column_name'])
                    # CASE prevents oversized bodies from crossing into Python.
                    # SQLite still reads storage to determine byte length; this
                    # is a transport/memory bound, not a disk-IO latency claim.
                    size = f'coalesce(length(CAST({column} AS BLOB)),0)'
                    remaining = min(MAX_VALUE_BYTES, MAX_BATCH_BYTES-snapshot_bytes)
                    rows = conn.execute(f'SELECT {size},CASE WHEN {size}<=? THEN {column} END FROM '
                                        f'{identifier(item["table_name"])} WHERE {where} LIMIT 2',
                                        (remaining, *values)).fetchall()
                    if len(rows) != 1:
                        item['read_error'] = 'source_identity_mismatch'
                    elif rows[0][0] > MAX_VALUE_BYTES:
                        item['read_error'] = 'source_value_budget_exceeded'
                    elif rows[0][0] > remaining:
                        # Leave this row dirty for the next bounded batch.
                        break
                    else:
                        item['value'] = rows[0][1]
                        snapshot_bytes += rows[0][0]
            snapshots.append(item)
    counts = {'processed': 0, 'stale': 0, 'errors': 0}
    for item in snapshots:
        edges, oracle_edges, error = set(), set(), item['read_error']
        try:
            if not item['deleted'] and not error:
                kinds = json.loads(item['kinds'])
                if 'json' in kinds:
                    edges.update(extract_json(item['value']))
                    oracle_edges.update(json_reference_digests(item['value']))
                if set(kinds) - {'json', 'inbound_fk', 'external_id'}:
                    raise ExtractionIncomplete('unknown extractor kind')
                if set(kinds) & {'inbound_fk', 'external_id'} and item['value'] is not None:
                    edges.add(reference_digest(item['value']))
                    oracle_edges.add(hashlib.sha256(item['value'].encode('utf-8')).hexdigest())
                if edges != oracle_edges:
                    error = 'shadow_edge_mismatch'
        except (ExtractionIncomplete, OracleIncomplete, UnicodeError):
            error = 'extraction_incomplete'
        with bounded_transaction(conn):
            current = conn.execute('''SELECT revision,deleted FROM retention_reference_rows
                WHERE source_id=? AND row_key=?''', (item['source_id'], item['row_key'])).fetchone()
            if current is None or tuple(current) != (item['revision'], item['deleted']):
                counts['stale'] += 1
                continue
            identity = (item['source_id'], item['row_key'])
            # An error keeps prior edges (conservative) and blocks readiness via
            # row state. Only a successful exact-version result replaces edges.
            if not error:
                conn.execute('DELETE FROM retention_reference_edges WHERE source_id=? AND row_key=?', identity)
                conn.executemany('INSERT INTO retention_reference_edges VALUES(?,?,?)',
                                 ((*identity, edge) for edge in sorted(edges)))
            conn.execute('''UPDATE retention_reference_rows SET state=?,value_digest=?,error_class=?,shadow_revision=?
                WHERE source_id=? AND row_key=? AND revision=?''',
                ('error' if error else 'complete', None if error else digest(item['value']),
                 error, None if error else item['revision'], *identity, item['revision']))
            conn.execute('DELETE FROM retention_reference_dirty WHERE source_id=? AND row_key=? AND revision=?',
                         (*identity, item['revision']))
            counts['processed'] += 1
            counts['errors'] += bool(error)
    return counts
