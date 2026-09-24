import sqlite3
from datetime import UTC, datetime

import pytest

from k3_support.body_retention import clear_unreferenced_page, preview
from k3_support.store import ingest_event


def test_preview_reports_new_fk_dependencies_without_exporting_bodies(conn):
    event, _ = ingest_event(conn, source="feishu_user_poll", identity="user", external_id="old",
        payload={"content": "PRIVATE BODY"}, occurred_at="2025-01-01T00:00:00Z")
    conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
    conn.execute("CREATE TABLE future_dependency(source TEXT REFERENCES inbound_events(event_pk))")
    conn.execute("INSERT INTO future_dependency VALUES(?)", (event,))
    before = conn.serialize()
    result = preview(conn, days=30, now=datetime(2026, 9, 8, tzinfo=UTC))
    assert conn.serialize() == before
    assert "PRIVATE BODY" not in str(result)
    item = result["items"][0]
    assert item["event_pk"] == event
    assert {"table": "future_dependency", "column": "source", "count": 1} in item["dependencies"]
    assert not item["processing_active"] and not item["deletion_allowed"]
    assert not preview(conn, days=30, after_id=event)["items"]


@pytest.mark.parametrize("days", [True, 0, -1, 3651, "30"])
def test_invalid_ttl_is_rejected(conn, days):
    with pytest.raises(ValueError):
        preview(conn, days=days)


def test_legacy_receipt_does_not_claim_error_details_were_cleared(conn):
    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='legacy-body',
                            payload={}, occurred_at='2025-01-01T00:00:00Z')
    conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed',last_error='legacy error' WHERE event_pk=?", (event,))
    conn.execute('''INSERT INTO body_retention_receipts
        (event_pk,preview_digest,payload_digest,body_bytes,retention_days,actor,cleared_at)
        VALUES(?,?,?,2,30,'legacy','2025-01-01')''', (event, 'a'*64, 'b'*64))
    item = preview(conn, days=30)['items'][0]
    assert item['clear_receipt']['last_error_digest'] is None
    assert item['clear_receipt']['last_error_bytes'] == 0
    assert item['last_error_bytes'] == len('legacy error')


@pytest.mark.parametrize("reference", ["internal", "external"])
def test_non_fk_knowledge_source_invalidates_preview(conn, reference):
    from test_knowledge_feedback import approved_knowledge

    from k3_support.store import create_case

    event, _ = ingest_event(conn, source="feishu_user_poll", identity="user", external_id="message-source",
        payload={"content": "private question"}, occurred_at="2025-01-01T00:00:00Z")
    conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
    before = preview(conn, days=30)["items"][0]
    case, _ = create_case(conn, title="reference", case_type="faq", severity="P3", confidence=0.9)
    knowledge = approved_knowledge(conn, case)
    conn.execute("INSERT INTO knowledge_sources(mapping_id,knowledge_id,source_type,stable_external_id,visibility,claim) VALUES(?,?,?,?,?,?)",
                 ("body-reference", knowledge, "feishu_im", event if reference == "internal" else "message-source", "internal", "private claim"))
    after = preview(conn, days=30)["items"][0]
    assert after["snapshot_digest"] != before["snapshot_digest"]
    assert {"table": "knowledge_sources", "column": "stable_external_id", "count": 1} in after["dependencies"]
    assert "private claim" not in str(after)
    assert not after["deletion_allowed"]


def test_nested_json_reference_is_counted_once_and_self_reference_excluded(conn):
    import json

    event, _ = ingest_event(conn, source="feishu_user_poll", identity="user", external_id="nested-message",
        payload={"content": "secret", "message_id": "nested-message"}, occurred_at="2025-01-01T00:00:00Z")
    conn.execute("UPDATE inbound_events SET received_epoch=0 WHERE event_pk=?", (event,))
    conn.execute("CREATE TABLE future_receipts(receipt_json TEXT)")
    baseline = preview(conn, days=30)["items"][0]
    conn.execute("INSERT INTO future_receipts VALUES(?)", (json.dumps({"nested": [event, event], "private": "SECRET"}),))
    conn.execute("INSERT INTO future_receipts VALUES('malformed JSON')")
    before = conn.serialize()
    item = preview(conn, days=30)["items"][0]
    assert conn.serialize() == before
    assert {"table": "future_receipts", "column": "receipt_json", "count": 1,
            "kind": "json_id_reference"} in item["dependencies"]
    assert not any(dep["table"] == "inbound_events" for dep in item["dependencies"])
    assert baseline["snapshot_digest"] != item["snapshot_digest"]
    assert "SECRET" not in str(item)


def test_page_scans_json_column_once_and_preserves_cross_target_matches(conn):
    import json

    from k3_support.body_retention import _json_dependencies

    conn.execute("CREATE TABLE batch_receipts(id TEXT PRIMARY KEY,receipt_json TEXT) WITHOUT ROWID")
    conn.executemany("INSERT INTO batch_receipts VALUES(?,?)", [
        ("one", json.dumps(["event-a", "event-a", "shared", {"event-c": "not-an-id"}])),
        ("two", json.dumps({"nested": ["event-b", "event-c"]})),
    ])
    events = [{"event_pk": "event-a", "external_id": "shared"},
              {"event_pk": "event-b", "external_id": "shared"},
              {"event_pk": "event-c", "external_id": "unique"}]
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        result, issues = _json_dependencies(conn, [("batch_receipts", "receipt_json")], events)
    finally:
        conn.set_trace_callback(None)
    assert len(statements) == 1
    assert issues == []
    assert [result[event["event_pk"]][0]["count"] for event in events] == [1, 2, 2]
    assert _json_dependencies(conn, [("batch_receipts", "receipt_json")], []) == ({}, [])


def test_cli_readonly_and_unreadable_json_are_explicit(conn, config, capsys):
    import json

    import yaml

    from k3_support.cli import main

    config.path.write_text(yaml.safe_dump(config.raw))
    event, _ = ingest_event(conn, source="timer", identity="system", external_id="old-timer",
        payload={}, occurred_at="2025-01-01T00:00:00Z")
    conn.execute("UPDATE inbound_events SET received_epoch=0 WHERE event_pk=?", (event,))
    conn.execute("CREATE TABLE broken_reference(data_json TEXT)")
    conn.execute("INSERT INTO broken_reference VALUES('PRIVATE MALFORMED')")
    before = conn.serialize()
    assert main(["--config", str(config.path), "body-retention-preview", "--days", "180"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["json_scan_issues"] == [{"table": "broken_reference", "column": "data_json", "unreadable_records": 1}]
    assert "PRIVATE MALFORMED" not in str(result)
    assert conn.serialize() == before


def test_cli_does_not_create_missing_database(config, capsys):
    import yaml

    from k3_support.cli import main

    config.path.write_text(yaml.safe_dump(config.raw))
    assert not config.database_path.exists()
    assert main(["--config", str(config.path), "body-retention-preview", "--days", "180"]) == 2
    assert "FileNotFoundError" in capsys.readouterr().err
    assert not config.database_path.exists()


def test_cli_does_not_migrate_outdated_schema(conn, config, capsys):
    import yaml

    from k3_support.cli import main

    config.path.write_text(yaml.safe_dump(config.raw))
    conn.execute("DELETE FROM schema_migrations WHERE version=(SELECT max(version) FROM schema_migrations)")
    before = conn.serialize()
    assert main(["--config", str(config.path), "body-retention-preview", "--days", "180"]) == 2
    assert "DatabaseError" in capsys.readouterr().err
    assert conn.serialize() == before


@pytest.mark.parametrize('budget', [{'max_records': 1}, {'max_bytes': 1}, {'max_nodes': 1}])
def test_dependency_scan_budget_never_reports_complete(conn, budget):
    from k3_support.body_retention import _json_dependencies

    conn.execute('CREATE TABLE large_receipts(body_json TEXT)')
    conn.executemany('INSERT INTO large_receipts VALUES(?)', [('"other"',), ('"event-a"',)])
    before = conn.serialize()
    _, issues = _json_dependencies(conn, [('large_receipts', 'body_json')],
        [{'event_pk': 'event-a', 'external_id': 'message-a'}], **budget)
    assert issues == [{'table': 'large_receipts', 'column': 'body_json',
                       'scan_incomplete': True, 'reason': 'dependency_scan_budget_exhausted'}]
    assert conn.serialize() == before


def test_single_json_record_traversal_respects_deadline(conn, monkeypatch):
    from k3_support.body_retention import _json_dependencies
    conn.execute('CREATE TABLE wide_receipt(body_json TEXT)')
    conn.execute('INSERT INTO wide_receipt VALUES(?)', ('["event-a","other"]',))
    # Start, row admission, first tree node, then time expires inside the row.
    ticks = iter([0, 0, 0, 11])
    monkeypatch.setattr('k3_support.body_retention.time.monotonic', lambda: next(ticks))
    before = conn.serialize()
    _, issues = _json_dependencies(conn, [('wide_receipt', 'body_json')],
        [{'event_pk': 'event-a', 'external_id': 'message-a'}])
    assert issues == [{'table': 'wide_receipt', 'column': 'body_json',
                       'scan_incomplete': True, 'reason': 'dependency_scan_budget_exhausted'}]
    assert conn.serialize() == before


@pytest.mark.parametrize('change', [None, 'body', 'error', 'reference', 'active', 'file', 'bad_json'])
def test_explicit_clear_revalidates_and_preserves_dedup(conn, change):
    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='clear-test',
        payload={'content': 'private old message'}, occurred_at='2025-01-01T00:00:00Z')
    conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
    conn.execute("UPDATE inbound_events SET last_error='private error 原文',attempt_count=3 WHERE event_pk=?", (event,))
    item = preview(conn, days=30)['items'][0]
    assert item['last_error_bytes'] == len('private error 原文'.encode())
    assert 'private error 原文' not in str(item)
    if change == 'body':
        conn.execute("UPDATE inbound_events SET payload_json='{}' WHERE event_pk=?", (event,))
    elif change == 'error':
        conn.execute("UPDATE inbound_events SET last_error='changed' WHERE event_pk=?", (event,))
    elif change == 'reference':
        conn.execute('CREATE TABLE late_reference(event TEXT REFERENCES inbound_events(event_pk))')
        conn.execute('INSERT INTO late_reference VALUES(?)', (event,))
    elif change == 'active':
        conn.execute("UPDATE inbound_events SET status='claimed' WHERE event_pk=?", (event,))
    elif change == 'file':
        conn.execute("UPDATE inbound_events SET raw_artifact_path='/not/read' WHERE event_pk=?", (event,))
    elif change == 'bad_json':
        conn.execute('CREATE TABLE invalid_dependency(body_json TEXT)')
        conn.execute("INSERT INTO invalid_dependency VALUES('invalid')")
    before = conn.serialize()
    kwargs = {'days': 30, 'expected': {event: item['snapshot_digest']}, 'actor': 'test-owner'}
    if change:
        with pytest.raises(ValueError):
            clear_unreferenced_page(conn, **kwargs)
        assert conn.serialize() == before
    else:
        assert clear_unreferenced_page(conn, **kwargs)['cleared'] == 1
        cleared = preview(conn, days=30)['items'][0]
        assert cleared['body_state'] == 'cleared_by_retention'
        assert cleared['clear_receipt']['actor'] == 'test-owner'
        assert cleared['clear_receipt']['body_bytes'] == item['body_bytes']
        assert cleared['clear_receipt']['last_error_bytes'] == item['last_error_bytes']
        assert cleared['clear_receipt']['last_error_digest']
        state = conn.execute('SELECT last_error,attempt_count,status FROM inbound_events WHERE event_pk=?', (event,)).fetchone()
        assert tuple(state) == (None, 3, 'processed')
        assert 'already_cleared' in cleared['clear_blockers']
        assert 'private old message' not in str(cleared)
        assert conn.execute('SELECT payload_json FROM inbound_events WHERE event_pk=?', (event,)).fetchone()[0] == '{}'
        assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 1
        _, created = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='clear-test',
            payload={'content': 'private old message'}, occurred_at='2025-01-01T00:00:00Z')
        assert not created
        with pytest.raises(ValueError):
            clear_unreferenced_page(conn, **kwargs)


@pytest.mark.parametrize('failure', ['stale_second', 'write_failure'])
def test_clear_batch_is_atomic(conn, failure):
    events = []
    for index in range(2):
        event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id=f'batch-{index}',
            payload={'content': f'private-{index}'}, occurred_at='2025-01-01T00:00:00Z')
        conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
        events.append(event)
    expected = {item['event_pk']: item['snapshot_digest'] for item in preview(conn, days=30)['items']}
    if failure == 'stale_second':
        conn.execute("UPDATE inbound_events SET status='new' WHERE event_pk=?", (list(expected)[1],))
    else:
        # The first payload update and both receipt inserts have happened when
        # this trigger aborts the second update. The enclosing transaction must
        # roll all of them back, not merely the failing SQL statement.
        conn.execute('''CREATE TRIGGER fail_second_clear BEFORE UPDATE OF payload_json ON inbound_events
            WHEN (SELECT count(*) FROM body_retention_receipts)=2
            BEGIN SELECT RAISE(ABORT, 'synthetic disk write failure'); END''')
    before = conn.serialize()
    with pytest.raises(ValueError if failure == 'stale_second' else sqlite3.IntegrityError):
        clear_unreferenced_page(conn, days=30, expected=expected, actor='test-owner')
    assert conn.serialize() == before
    assert not conn.in_transaction
    assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 0


@pytest.mark.parametrize('body_confirmed,error_confirmed', [(False, False), (True, False), (False, True), (True, True)])
def test_cli_clear_requires_explicit_confirmation(conn, config, capsys, body_confirmed, error_confirmed):
    import json

    import yaml

    from k3_support.cli import main

    config.path.write_text(yaml.safe_dump(config.raw))
    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='cli-clear',
        payload={'content': 'PRIVATE'}, occurred_at='2025-01-01T00:00:00Z')
    conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
    item = preview(conn, days=30)['items'][0]
    args = ['--config', str(config.path), 'body-retention-clear', '--days', '30',
            '--event', f"{event}:{item['snapshot_digest']}"]
    before = conn.serialize()
    confirmed = body_confirmed and error_confirmed
    if body_confirmed:
        args.append('--confirm-database-body-only')
    if error_confirmed:
        args.append('--confirm-error-details')
    assert main(args) == (0 if confirmed else 2)
    captured = capsys.readouterr()
    assert 'PRIVATE' not in captured.out + captured.err
    if confirmed:
        assert json.loads(captured.out)['cleared'] == 1
    else:
        assert conn.serialize() == before


def test_clear_cli_never_initializes_missing_database(config, capsys):
    import yaml

    from k3_support.cli import main

    config.path.write_text(yaml.safe_dump(config.raw))
    args = ['--config', str(config.path), 'body-retention-clear', '--days', '30',
            '--event', 'event:' + 'a' * 64, '--confirm-database-body-only', '--confirm-error-details']
    assert main(args) == 2
    assert 'FileNotFoundError' in capsys.readouterr().err
    assert not config.database_path.exists()


@pytest.mark.parametrize('invalid', ['schema', 'duplicate'])
def test_clear_cli_rejects_before_mutation(conn, config, capsys, invalid):
    import yaml

    from k3_support.cli import main

    config.path.write_text(yaml.safe_dump(config.raw))
    args = ['--config', str(config.path), 'body-retention-clear', '--days', '30',
            '--event', 'event:' + 'a' * 64, '--confirm-database-body-only', '--confirm-error-details']
    if invalid == 'schema':
        conn.execute('DELETE FROM schema_migrations WHERE version=85')
    else:
        args.extend(['--event', 'event:' + 'a' * 64])
    before = conn.serialize()
    assert main(args) == 2
    assert ('DatabaseError' if invalid == 'schema' else 'ValueError') in capsys.readouterr().err
    assert conn.serialize() == before


@pytest.mark.parametrize('encoded', ['{"EVENT": "metadata"}', '{"source":"EVENT","source":"other"}'])
def test_json_key_and_duplicate_key_references_block_clear(conn, encoded):
    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='json-key-reference',
        payload={'content': 'private body'}, occurred_at='2025-01-01T00:00:00Z')
    conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
    conn.execute('CREATE TABLE indexed_references(data_json TEXT)')
    conn.execute('INSERT INTO indexed_references VALUES(?)', (encoded.replace('EVENT', event),))
    item = preview(conn, days=30)['items'][0]
    assert 'referenced' in item['clear_blockers']
    before = conn.serialize()
    with pytest.raises(ValueError):
        clear_unreferenced_page(conn, days=30, expected={event: item['snapshot_digest']}, actor='test-owner')
    assert conn.serialize() == before
