"""Deadline-aware retention writer reservation with unconditional cleanup.

SQLite progress callbacks bound VM work, not an uninterruptible filesystem
operation or arbitrary Python callback. Deadline is also checked before commit.
Owns the connection's progress callback for the duration; do not nest handlers.
"""

import sqlite3
import time
from contextlib import contextmanager


class RetentionDeadline(TimeoutError):
    pass


@contextmanager
def bounded_transaction(conn, *, seconds=0.05):
    if type(seconds) not in (float, int) or not 0 < seconds <= 5:
        raise ValueError('invalid retention transaction deadline')
    if conn.in_transaction:
        raise ValueError('retention writer reservation cannot be nested')
    deadline = time.monotonic()+seconds
    prior_busy = conn.execute('PRAGMA busy_timeout').fetchone()[0]
    conn.execute(f'PRAGMA busy_timeout={min(prior_busy, max(1, int(seconds*1000)))}')
    def expired():
        return time.monotonic() >= deadline
    conn.set_progress_handler(lambda: int(expired()), 1000)
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        if expired():
            raise RetentionDeadline('retention transaction deadline exceeded')
        conn.execute('COMMIT')
    except BaseException as exc:
        # An interrupted SQLite statement may already have rolled back the
        # transaction. Disable cancellation before explicit rollback/cleanup.
        conn.set_progress_handler(None, 0)
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        if isinstance(exc, sqlite3.OperationalError) and expired():
            raise RetentionDeadline('retention transaction deadline exceeded') from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.execute(f'PRAGMA busy_timeout={prior_busy}')
