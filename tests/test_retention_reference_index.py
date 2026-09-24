import sqlite3

import pytest

from k3_support import db
from k3_support.retention_reference_index import begin_build, inspect_build
from k3_support.retention_reference_backfill import backfill_source
from k3_support.retention_reference_worker import consume_dirty


def index_db():
    conn = sqlite3.connect(':memory:', isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(next(sql for version, _, sql in db.migration_files() if version == 98))
    conn.execute('CREATE TABLE synthetic(id INTEGER PRIMARY KEY,payload_json TEXT)')
    conn.execute('INSERT INTO synthetic VALUES(1,\'["future"]\')')
    return conn


def test_global_install_precedes_backfill_and_never_invents_shadow_acceptance():
    conn = index_db()
    assert begin_build(conn)['sources'] == 1
    assert begin_build(conn)['generation'] == 1
    ident = conn.execute('SELECT source_id FROM retention_reference_sources').fetchone()[0]
    backfill_source(conn, ident)
    consume_dirty(conn)
    before = conn.total_changes
    status = inspect_build(conn)
    assert {b['reason'] for b in status['blockers']} == {'shadow_verification_required', 'shadow_scan_incomplete'}
    assert not status['ready']
    assert conn.total_changes == before


def test_removed_trigger_blocks_and_inspection_does_not_repair_it():
    conn = index_db()
    begin_build(conn)
    name = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone()[0]
    conn.execute(f'DROP TRIGGER "{name}"')
    before = conn.total_changes
    assert any(b['reason'] == 'capture trigger is missing' for b in inspect_build(conn)['blockers'])
    assert conn.total_changes == before
    with pytest.raises(ValueError, match='trigger is missing'):
        begin_build(conn)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is None


def test_schema_change_requires_explicit_rebuild():
    conn = index_db()
    begin_build(conn)
    conn.execute('ALTER TABLE synthetic ADD COLUMN unknown_json TEXT')
    assert any(b['reason'] == 'schema_digest_mismatch' for b in inspect_build(conn)['blockers'])
    with pytest.raises(ValueError, match='rebuild'):
        begin_build(conn)


def test_full_project_capture_installation(conn):
    from k3_support.retention_reference_sources import inventory
    report = inventory(conn)
    assert not report['issues'], report['issues']
    result = begin_build(conn)
    assert result['sources'] == len(report['sources'])
    assert result['sources'] > 50
    status = inspect_build(conn)
    assert not status['ready']
    assert {b['reason'] for b in status['blockers']} <= {'shadow_verification_required', 'backfill_incomplete', 'shadow_scan_incomplete'}
