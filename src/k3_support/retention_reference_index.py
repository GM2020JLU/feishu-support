"""Global capture installation and fail-closed index diagnostics.

Readiness still requires independent shadow verification; this module cannot
promote a generation to ready merely because its work queues are empty.
"""

from contextlib import nullcontext

from .db import transaction
from .ids import digest
from .retention_reference_capture import install_source
from .retention_reference_sources import inventory


def begin_build(conn):
    with transaction(conn):
        report = inventory(conn)
        if report['issues']:
            raise ValueError('reference inventory is incomplete')
        generation = conn.execute('SELECT * FROM retention_reference_generations ORDER BY generation DESC LIMIT 1').fetchone()
        if generation:
            if any(generation[key] != report[key] for key in ('schema_digest', 'coverage_digest', 'extractor_version')):
                raise ValueError('reference generation changed; explicit rebuild required')
            if generation['state'] == 'invalid':
                raise ValueError('reference generation is invalid')
        for source in report['sources']:
            install_source(conn, source, verify_only=generation is not None)
        if generation is None:
            conn.execute('''INSERT INTO retention_reference_generations
                (generation,state,schema_digest,coverage_digest,extractor_version)
                VALUES(1,'building',?,?,?)''',
                (report['schema_digest'], report['coverage_digest'], report['extractor_version']))
        return {'generation': generation['generation'] if generation else 1,
                'sources': len(report['sources']), 'ready': False}


def inspect_build(conn):
    with nullcontext(conn) if conn.in_transaction else transaction(conn, immediate=False):
        report = inventory(conn)
        blockers = list(report['issues'])
        generation = conn.execute('SELECT * FROM retention_reference_generations ORDER BY generation DESC LIMIT 1').fetchone()
        if generation is None:
            blockers.append({'reason': 'generation_missing'})
        else:
            for key in ('schema_digest', 'coverage_digest', 'extractor_version'):
                if generation[key] != report[key]:
                    blockers.append({'reason': key+'_mismatch'})
            if generation['state'] != 'ready' or not generation['shadow_digest']:
                blockers.append({'reason': 'shadow_verification_required'})
        for source in report['sources']:
            try:
                install_source(conn, source, verify_only=True)
            except ValueError as exc:
                blockers.append({'table': source.table, 'column': source.column, 'reason': str(exc)})
        for table, predicate, reason in (
            ('retention_reference_sources', 'backfill_complete=0', 'backfill_incomplete'),
            ('retention_reference_dirty', '1', 'dirty_pending'),
            ('retention_reference_rows', "state!='complete'", 'row_incomplete'),
            ('retention_reference_sources', 'shadow_complete=0 OR shadow_error IS NOT NULL', 'shadow_scan_incomplete'),
            ('retention_reference_rows', 'shadow_revision IS NULL OR shadow_revision!=revision', 'shadow_rows_stale'),
        ):
            if conn.execute(f'SELECT 1 FROM {table} WHERE {predicate} LIMIT 1').fetchone():
                blockers.append({'reason': reason})
        return {'ready': not blockers, 'blockers': blockers,
                'generation': generation['generation'] if generation else None}


def promote_verified(conn):
    """Atomically mark the index ready only after complete independent coverage.

    Ready is an index fact, not permission to delete a target or bypass policy.
    Every deletion must still inspect current dirty/version/policy state.
    """
    with transaction(conn):
        status = inspect_build(conn)
        blockers = [b for b in status['blockers'] if b['reason'] != 'shadow_verification_required']
        row = conn.execute('SELECT * FROM retention_reference_generations WHERE generation=?',
                           (status['generation'],)).fetchone()
        if blockers or row is None or row['state'] not in ('building', 'ready'):
            raise ValueError('reference index has incomplete or stale verification')
        source_receipts = [tuple(item) for item in conn.execute('''SELECT source_id,schema_digest,
            extractor_version,backfill_cursor,shadow_cursor FROM retention_reference_sources ORDER BY source_id''')]
        receipt = digest({'generation': row['generation'], 'schema': row['schema_digest'],
                          'coverage': row['coverage_digest'], 'extractor': row['extractor_version'],
                          'sources': source_receipts, 'oracle': 'sqlite-json-tree-v1'})
        conn.execute("UPDATE retention_reference_generations SET state='ready',shadow_digest=? WHERE generation=?",
                     (receipt, row['generation']))
        return {'generation': row['generation'], 'shadow_digest': receipt, 'ready': True}
