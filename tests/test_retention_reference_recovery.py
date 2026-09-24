import pytest
from test_retention_reference_index import index_db

from k3_support.retention_reference_maintenance import tick
from k3_support.retention_reference_recovery import retry_source
from k3_support.retention_reference_sources import inventory


def failed():
    conn = index_db()
    conn.execute("UPDATE synthetic SET payload_json='invalid'")
    assert not tick(conn, max_batches=1, max_seconds=10)['ready']
    ident = conn.execute('SELECT source_id FROM retention_reference_sources').fetchone()[0]
    return conn, ident


def test_fixed_source_recovery_resumes_without_resetting_backfill():
    conn, ident = failed()
    before = conn.execute('SELECT backfill_cursor FROM retention_reference_sources').fetchone()[0]
    conn.execute("UPDATE synthetic SET payload_json='[\"fixed\"]'")
    schema = inventory(conn)['schema_digest']
    assert not retry_source(conn, ident, expected_schema_digest=schema)['ready']
    assert tick(conn, max_batches=5, max_seconds=10)['ready']
    assert conn.execute('SELECT backfill_cursor FROM retention_reference_sources').fetchone()[0] == before


def test_retry_does_not_make_still_invalid_source_ready():
    conn, ident = failed()
    schema = inventory(conn)['schema_digest']
    assert retry_source(conn, ident, expected_schema_digest=schema)['requeued'] == 1
    assert not tick(conn, max_batches=5, max_seconds=10)['ready']
    assert conn.execute('SELECT state FROM retention_reference_rows').fetchone()[0] == 'error'


def test_schema_change_rejects_recovery_without_writes():
    conn, ident = failed()
    schema = inventory(conn)['schema_digest']
    conn.execute('ALTER TABLE synthetic ADD COLUMN new_json TEXT')
    before = conn.total_changes
    with pytest.raises(ValueError, match='schema changed'):
        retry_source(conn, ident, expected_schema_digest=schema)
    assert conn.total_changes == before
