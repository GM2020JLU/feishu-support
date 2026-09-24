"""Independent, bounded SQLite JSON oracle for reference shadow checks.

This deliberately does not call the Python production extractor. It does not
authorize a generation: callers must bind full source coverage and row versions.
"""

import hashlib
import sqlite3


class OracleIncomplete(ValueError):
    pass


def json_reference_digests(value, *, max_bytes=1024 * 1024, max_nodes=100_000):
    if type(max_bytes) is not int or max_bytes < 1 or type(max_nodes) is not int or max_nodes < 1:
        raise ValueError('invalid oracle budget')
    if value is None:
        return frozenset()
    if not isinstance(value, str):
        raise OracleIncomplete('non-text JSON')
    try:
        encoded = value.encode('utf-8')
    except UnicodeError as exc:
        raise OracleIncomplete('unsupported JSON encoding') from exc
    if len(encoded) > max_bytes:
        raise OracleIncomplete('oracle byte budget exceeded')
    # Own connection: no progress-handler interference with a caller's database,
    # and no possibility of executing SQL against application payload tables.
    conn = sqlite3.connect(':memory:')
    instructions = 0
    def progress():
        nonlocal instructions
        instructions += 1000
        return int(instructions > max_nodes * 100 + 10_000)
    conn.set_progress_handler(progress, 1000)
    result = set()
    try:
        if conn.execute('SELECT json_valid(?)', (value,)).fetchone()[0] != 1:
            raise OracleIncomplete('invalid JSON')
        for count, (key, kind, atom) in enumerate(conn.execute(
            'SELECT key,type,atom FROM json_tree(?)', (value,)
        ), start=1):
            if count > max_nodes:
                raise OracleIncomplete('oracle node budget exceeded')
            for candidate in (key if isinstance(key, str) else None, atom if kind == 'text' else None):
                if candidate is not None:
                    result.add(hashlib.sha256(candidate.encode('utf-8')).hexdigest())
    except (sqlite3.Error, UnicodeError) as exc:
        raise OracleIncomplete('SQLite oracle could not verify JSON') from exc
    finally:
        conn.close()
    return frozenset(result)
