"""Retry failed extraction without discarding protective index evidence."""

from .retention_reference_capture import install_source
from .retention_reference_sources import inventory
from .retention_transaction import bounded_transaction


def retry_source(conn, source_id, *, expected_schema_digest, limit=100):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid recovery batch limit')
    with bounded_transaction(conn):
        report = inventory(conn)
        if report['issues'] or report['schema_digest'] != expected_schema_digest:
            raise ValueError('reference recovery schema changed')
        generation = conn.execute('SELECT * FROM retention_reference_generations ORDER BY generation DESC LIMIT 1').fetchone()
        if generation is None or any(generation[key] != report[key] for key in
                                     ('schema_digest', 'coverage_digest', 'extractor_version')):
            raise ValueError('reference recovery generation changed')
        source = conn.execute('SELECT * FROM retention_reference_sources WHERE source_id=?', (source_id,)).fetchone()
        if source is None:
            raise ValueError('unknown recovery source')
        selected = next((item for item in report['sources']
                         if (item.table, item.column) == (source['table_name'], source['column_name'])), None)
        if selected is None:
            raise ValueError('recovery source not covered')
        install_source(conn, selected, verify_only=True)
        rows = conn.execute('''SELECT row_key,revision FROM retention_reference_rows
            WHERE source_id=? AND state='error' ORDER BY row_key LIMIT ?''', (source_id, limit)).fetchall()
        for row in rows:
            conn.execute('''UPDATE retention_reference_rows SET state='pending',error_class=NULL,shadow_revision=NULL
                WHERE source_id=? AND row_key=? AND revision=?''', (source_id, row[0], row[1]))
            conn.execute('''INSERT INTO retention_reference_dirty VALUES(?,?,?)
                ON CONFLICT(source_id,row_key) DO UPDATE SET revision=excluded.revision''',
                (source_id, row[0], row[1]))
        # Resume the independent scan at the checkpoint preceding its failure.
        # No reference edges, tombstones, or source cursors are removed.
        conn.execute('UPDATE retention_reference_sources SET shadow_error=NULL WHERE source_id=?', (source_id,))
        return {'requeued': len(rows), 'ready': False, 'source_id': source_id}
