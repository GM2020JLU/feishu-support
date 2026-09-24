"""Persistent keyset backfill into the same versioned dirty protocol."""

import json

from .retention_transaction import bounded_transaction
from .retention_reference_sources import identifier


def backfill_source(conn, source_id, *, limit=100):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('backfill batch limit must be between 1 and 1000')
    with bounded_transaction(conn):
        source = conn.execute('SELECT * FROM retention_reference_sources WHERE source_id=?',
                              (source_id,)).fetchone()
        if source is None:
            raise ValueError('reference source is not registered')
        if source['backfill_complete']:
            return {'scanned': 0, 'complete': True}
        keys = json.loads(source['key_spec'])
        if not keys:
            raise ValueError('reference source has no stable primary key')
        columns = ','.join(identifier(key) for key in keys)
        cursor = json.loads(source['backfill_cursor']) if source['backfill_cursor'] else None
        where, params = '', []
        if cursor is not None:
            if len(cursor) != len(keys) or any(value is None for value in cursor):
                raise ValueError('invalid backfill checkpoint')
            # SQLite row-value comparison respects each primary key collation.
            left = f'({columns})' if len(keys) > 1 else columns
            right = '(' + ','.join('?' for _ in keys) + ')' if len(keys) > 1 else '?'
            where, params = f' WHERE {left}>{right}', cursor
        rows = conn.execute(f'SELECT json_array({columns}),{columns} FROM '
                            f'{identifier(source["table_name"])}{where} ORDER BY {columns} LIMIT ?',
                            (*params, limit+1)).fetchall()
        for row in rows[:limit]:
            if any(value is None for value in tuple(row)[1:]):
                raise ValueError('null source primary key blocks backfill')
            row_key = row[0]
            # Do not reset a revision created by insert/update/delete capture.
            conn.execute('''INSERT INTO retention_reference_rows
                (source_id,row_key,revision,deleted,state) VALUES(?,?,1,0,'pending')
                ON CONFLICT(source_id,row_key) DO NOTHING''', (source_id, row_key))
            conn.execute('''INSERT INTO retention_reference_dirty(source_id,row_key,revision)
                SELECT source_id,row_key,revision FROM retention_reference_rows
                WHERE source_id=? AND row_key=? AND state='pending'
                ON CONFLICT(source_id,row_key) DO UPDATE SET revision=excluded.revision''',
                (source_id, row_key))
        complete = len(rows) <= limit
        checkpoint = rows[min(limit, len(rows))-1][0] if rows else source['backfill_cursor']
        conn.execute('''UPDATE retention_reference_sources SET backfill_cursor=?,backfill_complete=?
            WHERE source_id=?''', (checkpoint, int(complete), source_id))
    return {'scanned': min(limit, len(rows)), 'complete': complete}
