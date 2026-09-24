import sqlite3

from k3_support.retention_reference_sources import inventory


def test_inventory_covers_non_event_json_fk_and_external_registry():
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE inbound_events(event_pk TEXT PRIMARY KEY, payload_json TEXT);
        CREATE TABLE other(a TEXT, b INTEGER, result_json TEXT,
            event_id TEXT REFERENCES inbound_events(event_pk), PRIMARY KEY(b,a));
        CREATE TABLE knowledge_sources(source_id TEXT PRIMARY KEY, stable_external_id TEXT);
    ''')
    report = inventory(conn)
    sources = {(s.table, s.column): s for s in report['sources']}
    assert sources['other', 'result_json'].primary_key == ('b', 'a')
    assert sources['other', 'event_id'].kinds == ('inbound_fk',)
    assert sources['knowledge_sources', 'stable_external_id'].kinds == ('external_id',)
    assert not report['issues']
    assert not report['ready']


def test_new_source_changes_coverage_and_schema_and_missing_identity_blocks():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE original(id INTEGER PRIMARY KEY, payload_json TEXT)')
    before = inventory(conn)
    conn.execute('CREATE TABLE future(payload_json TEXT)')
    after = inventory(conn)
    assert before['schema_digest'] != after['schema_digest']
    assert before['coverage_digest'] != after['coverage_digest']
    assert after['issues'] == [{'table': 'future', 'column': 'payload_json',
                                'reason': 'missing_stable_primary_key'}]


def test_unrelated_schema_change_invalidates_schema_even_without_new_reference():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE original(id INTEGER PRIMARY KEY, payload_json TEXT)')
    before = inventory(conn)
    conn.execute('CREATE TABLE future(id INTEGER PRIMARY KEY, unknown_text TEXT)')
    assert inventory(conn)['schema_digest'] != before['schema_digest']


def test_quoted_identifiers_and_missing_registry_field_fail_closed():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE "odd""name"(id INTEGER PRIMARY KEY, "odd""_json" TEXT)')
    conn.execute('CREATE TABLE case_sources(source_id TEXT PRIMARY KEY)')
    report = inventory(conn)
    assert report['sources'][0].table == 'odd"name'
    assert report['issues'] == [{'table': 'case_sources', 'reason': 'missing_stable_external_id'}]


def test_migrated_project_inventory_covers_every_json_column(conn):
    report = inventory(conn)
    discovered = {(s.table, s.column) for s in report['sources'] if 'json' in s.kinds}
    expected = set()
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        quoted = '"' + table.replace('"', '""') + '"'
        expected.update((table, row[1]) for row in conn.execute(f'PRAGMA table_info({quoted})')
                        if row[1].endswith('_json'))
    assert discovered == expected
    assert len(discovered) > 50
    assert not report['ready']


def test_schema_cache_does_not_trust_reused_schema_version():
    conn = sqlite3.connect(':memory:', isolation_level=None)
    conn.execute('CREATE TABLE original(id INTEGER PRIMARY KEY,payload_json TEXT)')
    baseline = inventory(conn)
    conn.execute('BEGIN')
    conn.execute('CREATE TABLE transient(id INTEGER PRIMARY KEY,payload_json TEXT)')
    transient = inventory(conn)
    version = conn.execute('PRAGMA schema_version').fetchone()[0]
    conn.execute('ROLLBACK')
    conn.execute('CREATE TABLE different(id INTEGER PRIMARY KEY,payload_json TEXT)')
    assert conn.execute('PRAGMA schema_version').fetchone()[0] == version
    current = inventory(conn)
    assert current['schema_digest'] not in (baseline['schema_digest'], transient['schema_digest'])
    assert any(s.table == 'different' for s in current['sources'])
    assert not any(s.table == 'transient' for s in current['sources'])


def test_cache_result_mutation_does_not_poison_next_inventory():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE original(id INTEGER PRIMARY KEY,payload_json TEXT)')
    first = inventory(conn)
    first['sources'].clear()
    assert len(inventory(conn)['sources']) == 1
