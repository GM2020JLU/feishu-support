import sqlite3
import time

import pytest

from k3_support.retention_transaction import bounded_transaction, RetentionDeadline


def test_precommit_deadline_rolls_back_and_restores_connection(monkeypatch):
    from k3_support import retention_transaction as module
    conn = sqlite3.connect(':memory:', isolation_level=None)
    conn.execute('CREATE TABLE sample(id INTEGER)')
    conn.execute('PRAGMA busy_timeout=1234')
    now = [10.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    with pytest.raises(RetentionDeadline):
        with bounded_transaction(conn, seconds=0.05):
            conn.execute('INSERT INTO sample VALUES(1)')
            now[0] += 1
    assert not conn.in_transaction
    assert conn.execute('SELECT count(*) FROM sample').fetchone()[0] == 0
    assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 1234
    with bounded_transaction(conn):
        conn.execute('INSERT INTO sample VALUES(2)')
    assert conn.execute('SELECT id FROM sample').fetchone()[0] == 2


def test_progress_interrupt_cleans_up_already_rolled_back_transaction(monkeypatch):
    from k3_support import retention_transaction as module
    conn = sqlite3.connect(':memory:', isolation_level=None)
    conn.execute('CREATE TABLE sample(id INTEGER)')
    now = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    with pytest.raises(RetentionDeadline):
        with bounded_transaction(conn):
            conn.execute('INSERT INTO sample VALUES(1)')
            now[0] = 1
            conn.execute('''WITH RECURSIVE sequence(n) AS
                (SELECT 1 UNION ALL SELECT n+1 FROM sequence WHERE n<1000000)
                INSERT INTO sample SELECT n FROM sequence''')
    assert not conn.in_transaction
    assert conn.execute('SELECT count(*) FROM sample').fetchone()[0] == 0


def test_busy_writer_does_not_inherit_five_second_wait(tmp_path):
    from k3_support.db import connect, transaction
    first = connect(tmp_path / 'contended.db')
    second = connect(tmp_path / 'contended.db')
    try:
        first.execute('CREATE TABLE sample(id INTEGER)')
        with transaction(first):
            first.execute('INSERT INTO sample VALUES(1)')
            started = time.monotonic()
            with pytest.raises((RetentionDeadline, sqlite3.OperationalError)):
                with bounded_transaction(second, seconds=0.02):
                    second.execute('INSERT INTO sample VALUES(2)')
            assert time.monotonic()-started < 0.5
        assert not second.in_transaction
        assert second.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
        assert second.execute('SELECT count(*) FROM sample').fetchone()[0] == 1
    finally:
        first.close()
        second.close()
