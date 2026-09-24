"""Bounded maintenance orchestration, separate from target deletion."""

import time

from .retention_reference_backfill import backfill_source
from .retention_reference_index import begin_build, inspect_build, promote_verified
from .retention_reference_shadow import scan_source
from .retention_reference_worker import consume_dirty


def tick(conn, *, max_batches=20, max_seconds=0.5):
    if type(max_batches) is not int or not 1 <= max_batches <= 100:
        raise ValueError('invalid maintenance batch budget')
    if type(max_seconds) not in (int, float) or not 0 < max_seconds <= 30:
        raise ValueError('invalid maintenance time budget')
    deadline = time.monotonic()+max_seconds
    counts = {'backfilled': 0, 'consumed': 0, 'shadow_scanned': 0, 'errors': 0}
    begin_build(conn)
    for _ in range(max_batches):
        if time.monotonic() >= deadline:
            break
        progressed = False
        source = conn.execute('''SELECT source_id FROM retention_reference_sources
            WHERE backfill_complete=0 ORDER BY source_id LIMIT 1''').fetchone()
        if source:
            result = backfill_source(conn, source[0], limit=100)
            counts['backfilled'] += result['scanned']
            progressed = True  # Empty-source checkpoint completion is progress.
        if time.monotonic() >= deadline:
            break
        result = consume_dirty(conn, limit=100)
        counts['consumed'] += result['processed']
        counts['errors'] += result['errors']
        progressed |= bool(result['processed'] or result['stale'])
        if time.monotonic() >= deadline:
            break
        source = conn.execute('''SELECT source_id FROM retention_reference_sources
            WHERE backfill_complete=1 AND shadow_complete=0 AND shadow_error IS NULL
            ORDER BY source_id LIMIT 1''').fetchone()
        if source:
            result = scan_source(conn, source[0], limit=10)
            counts['shadow_scanned'] += result['scanned']
            counts['errors'] += bool(result.get('error'))
            progressed = True
        if not progressed:
            break
    status = inspect_build(conn)
    if {b['reason'] for b in status['blockers']} == {'shadow_verification_required'}:
        try:
            promote_verified(conn)
        except ValueError:
            # Capture may commit between the read-only diagnostic and the
            # atomic promotion check. Preserve that blocker and continue next
            # tick; do not misclassify a normal write race as broken recovery.
            current = inspect_build(conn)
            if not any(b['reason'] in ('dirty_pending', 'row_incomplete', 'shadow_rows_stale')
                       for b in current['blockers']):
                raise
            return {**counts, **current}
        status = inspect_build(conn)
    return {**counts, **status}
