import pytest

from k3_support.case_content_inventory import preview
from k3_support.store import create_case, ingest_event


@pytest.mark.parametrize('sql,index', [
    ('SELECT case_id FROM conversation_contexts WHERE focus_event_pk=?','idx_context_focus_source'),
    ('SELECT context_id FROM conversation_anchor_aliases WHERE source_event_pk=?','idx_context_alias_source'),
])
def test_reverse_context_source_lookup_uses_index(conn, sql, index):
    plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN '+sql,('fixture',))]
    assert any('SEARCH' in line and index in line for line in plan)
    assert not any('SCAN ' in line for line in plan)


def test_context_index_migration_rolls_back_partial_upgrade(conn):
    from k3_support.db import migrate, DatabaseError
    conn.execute('DROP INDEX idx_context_focus_source')
    conn.execute('DELETE FROM schema_migrations WHERE version=94')
    # Keep the second index to induce a conflict after creating the first one.
    before = conn.serialize()
    with pytest.raises(DatabaseError, match='094_retention_context_source_indexes'):
        migrate(conn)
    assert conn.serialize() == before
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_context_focus_source'").fetchone() is None
    assert conn.execute('SELECT 1 FROM schema_migrations WHERE version=94').fetchone() is None
    conn.execute('DROP INDEX idx_context_alias_source')
    conn.execute('DROP INDEX idx_context_case_retention')
    assert migrate(conn) == [94]
    completed = conn.serialize()
    assert migrate(conn) == [] and conn.serialize() == completed


def test_shared_source_lookup_does_not_scan_thousands_of_unrelated_contexts(conn):
    from k3_support.case_content_inventory import shared_source_count
    event, _ = ingest_event(conn,source='feishu_user_poll',identity='user',external_id='scale-message',
                           payload={'text':'PRIVATE'},occurred_at='2026-01-01T00:00:00Z')
    case, _ = create_case(conn,title='fixture',case_type='faq',severity='P3',confidence=.9,source_event_pk=event)
    conn.execute('''WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<5000)
        INSERT INTO conversation_contexts(context_id,chat_id,chat_type,input_digest,created_at,updated_at)
        SELECT 'scale-'||x,'chat-'||x,'p2p','digest','now','now' FROM n''')
    conn.execute("UPDATE conversation_contexts SET focus_event_pk=? WHERE context_id='scale-5000'", (event,))
    ticks = []
    def budget():
        ticks.append(1)
        return int(len(ticks)>100)
    conn.set_progress_handler(budget,100)
    try:
        assert shared_source_count(conn,case) == 1
    finally:
        conn.set_progress_handler(None,0)
    # Bound VM work, not a machine-specific wall-clock benchmark.
    assert len(ticks)<=100


def test_text_transform_preview_counts_bytes_without_returning_payload(conn):
    from k3_support.store import enqueue_outbox
    case, _ = create_case(conn, title='fixture', case_type='faq', severity='P3', confidence=.9)
    for key, payload in [('known', {'text':'保密正文', 'identity':'user'}),
                         ('release', {'text':'PRIVATE RELEASE', 'identity':'user',
                                      'knowledge_release':{'provenance':{'knowledge_scope_facts':{'text':'PRIVATE SOURCE'}}}}),
                         ('unknown', {'text':'OTHER PRIVATE', 'identity':'user', 'unknown':True})]:
        enqueue_outbox(conn,channel='feishu_im',action_type='reply',destination='fixture',
                       payload=payload,idempotency_key=key,case_id=case)
    before = conn.serialize()
    result = preview(conn, case)
    fields = result['outbox_json_fields']
    assert fields['candidate_transform_rows'] == 1
    assert fields['candidate_removed_bytes'] == len('保密正文'.encode())
    assert fields['unclassified_rows'] == 2 and not fields['clear_allowed']
    assert fields['release_provenance_review_rows'] == 1
    assert 'PRIVATE RELEASE' not in str(result) and 'PRIVATE SOURCE' not in str(result)
    assert '保密正文' not in str(result) and 'OTHER PRIVATE' not in str(result)
    assert conn.serialize() == before


def test_mail_summary_is_counted_once_and_retained_independently(conn):
    case, _ = create_case(conn, title='mail', case_type='faq', severity='P3', confidence=.9)
    conn.execute('''INSERT INTO mail_items(message_id,case_id,received_at,updated_at)
        VALUES('mail',?,'now','now')''', (case,))
    conn.execute('''INSERT INTO mail_digest_runs(digest_id,summary_type,watermark_key,
        range_end,item_count,ai_summary_json,content_digest,telegram_destination,state,created_at)
        VALUES('digest','mail_noon','watermark','end',2,'{"summary":"PRIVATE"}',
        'digest','destination','linking','now')''')
    for ordinal, message in enumerate(['mail', 'unrelated-mail']):
        conn.execute('''INSERT INTO mail_summary_membership VALUES('digest',?,?,
            'other','information',NULL,0,'{"subject":"PRIVATE"}')''', (message, ordinal))
    conn.execute('''INSERT INTO outbox(outbox_id,channel,action_type,destination,payload_json,
        idempotency_key,state,created_at,updated_at)
        VALUES('summary-send','telegram','reply','private','{"text":"PRIVATE"}',
               'summary-key','pending','now','now')''')
    conn.execute("UPDATE mail_digest_runs SET telegram_outbox_id='summary-send' WHERE digest_id='digest'")
    before = conn.serialize()
    result = preview(conn, case)
    surfaces = {row['table']: row for row in result['surfaces']}
    assert surfaces['mail_digest_runs']['rows_counted'] == 1
    assert surfaces['mail_summary_membership']['rows_counted'] == 1
    assert surfaces['outbox']['rows_counted'] == 1
    assert {'reason': 'delivery_unsettled', 'count': 1} in result['observed_holds']
    assert {'reason': 'mail_summary_reference', 'count': 1} in result['observed_holds']
    assert 'PRIVATE' not in str(result)
    assert conn.serialize() == before
    root, _ = create_case(conn,title='root',case_type='faq',severity='P3',confidence=.9)
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (root,case))
    before = conn.serialize()
    group = preview(conn,root)['canonical_group_holds']
    member = next(row for row in group['members'] if row['case_id']==case)
    assert {'reason':'mail_summary_reference','count':1} in member['dependency_holds']
    assert 'PRIVATE' not in str(group) and conn.serialize() == before


@pytest.mark.parametrize('state,held', [('pending', True), ('delivered', False)])
def test_context_outbox_without_case_id_is_counted_and_held(conn, state, held):
    case, _ = create_case(conn, title='context', case_type='faq', severity='P3', confidence=.9)
    conn.execute('''INSERT INTO conversation_contexts(context_id,chat_id,chat_type,case_id,
        input_digest,created_at,updated_at) VALUES('ctx','chat','p2p',?,'digest','now','now')''', (case,))
    for key, context in [('related', 'ctx'), ('unrelated', None)]:
        conn.execute('''INSERT INTO outbox(outbox_id,channel,action_type,destination,payload_json,
            idempotency_key,state,context_id,created_at,updated_at)
            VALUES(?,'telegram','reply','private','{}',?,?,?,'now','now')''', (key,key,state,context))
    result = preview(conn, case)
    assert next(r for r in result['surfaces'] if r['table'] == 'outbox')['rows_counted'] == 1
    assert any(h['reason'] == 'delivery_unsettled' for h in result['observed_holds']) == held


@pytest.mark.parametrize('relation', ['focus', 'member', 'alias'])
def test_context_only_source_messages_are_counted(conn, relation):
    case, _ = create_case(conn, title='context', case_type='faq', severity='P3', confidence=.9)
    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user',
        external_id='context-only-message', payload={'text': 'PRIVATE CONTEXT SOURCE'},
        occurred_at='2026-09-09T00:00:00+00:00')
    conn.execute('''INSERT INTO conversation_contexts(context_id,chat_id,chat_type,case_id,
        input_digest,created_at,updated_at) VALUES('ctx','chat','p2p',?,'digest','now','now')''', (case,))
    if relation == 'focus':
        conn.execute("UPDATE conversation_contexts SET focus_event_pk=? WHERE context_id='ctx'", (event,))
    elif relation == 'member':
        conn.execute('''INSERT INTO conversation_context_members VALUES(?,'ctx',1,'e','c',
            'colleague','same','now')''', (event,))
    else:
        conn.execute("INSERT INTO conversation_anchor_aliases VALUES('chat','alias','ctx',?)", (event,))
    before = conn.serialize()
    result = preview(conn, case)
    source = next(r for r in result['surfaces'] if r['table'] == 'inbound_events')
    assert source['rows_counted'] == 1
    assert source['bytes_counted'] > 0
    assert 'PRIVATE CONTEXT SOURCE' not in str(result)
    assert conn.serialize() == before


def test_later_case_copies_include_mail_and_rounds_without_other_cases(conn):
    case, _ = create_case(conn, title='one', case_type='faq', severity='P3', confidence=.9)
    other, _ = create_case(conn, title='two', case_type='faq', severity='P3', confidence=.9)
    for key, owner in [('mail-one', case), ('mail-two', other)]:
        conn.execute('''INSERT INTO mail_items(message_id,case_id,subject,body_preview,
            received_at,updated_at) VALUES(?,?,?,?,?,?)''',
            (key, owner, '私密主题', '私密正文', 'now', 'now'))
    conn.execute('UPDATE case_rounds SET reason=? WHERE case_id=?', ('私密轮次原因', case))
    before = conn.serialize()
    result = preview(conn, case)
    rows = {row['table']: row for row in result['surfaces']}
    assert rows['mail_items']['rows_counted'] == 1
    assert rows['mail_items']['bytes_counted'] == len('私密主题私密正文'.encode())
    assert rows['case_rounds']['bytes_counted'] == len('私密轮次原因'.encode())
    assert '私密' not in str(result)
    assert before == conn.serialize()
    assert not result['coverage_complete'] and not result['deletion_allowed']


def test_bounded_inventory_interrupts_sql_and_restores_connection(conn, monkeypatch):
    from k3_support import case_content_inventory as module
    def expensive(conn, *args, **kwargs):
        return conn.execute('''WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL
            SELECT x+1 FROM n WHERE x<10000000) SELECT sum(x) FROM n''').fetchone()
    monkeypatch.setattr(module, 'preview', expensive)
    with pytest.raises(ValueError, match='时间预算'):
        module.bounded_preview(conn, 'case', seconds=.00001)
    assert conn.execute('SELECT 1').fetchone()[0] == 1


@pytest.mark.parametrize('seconds', [True, 0, -1, 6, float('nan'), float('inf')])
def test_invalid_query_budget(conn, seconds):
    from k3_support.case_content_inventory import bounded_preview
    with pytest.raises(ValueError):
        bounded_preview(conn, 'case', seconds=seconds)


def test_inventory_counts_bytes_without_exposing_or_changing_content(conn):
    case, _ = create_case(conn, title='私密内容', case_type='faq', severity='P3', confidence=.9)
    before = conn.serialize()
    result = preview(conn, case)
    assert conn.serialize() == before
    assert '私密内容' not in str(result)
    row = next(item for item in result['surfaces'] if item['table'] == 'cases')
    assert row['bytes_counted'] == len('私密内容'.encode())
    assert row['rows_counted'] == 1
    assert not result['deletion_allowed'] and not result['coverage_complete']


def test_inventory_reports_linked_case_without_following_or_deleting(conn):
    first, _ = create_case(conn, title='one', case_type='faq', severity='P3', confidence=.9)
    second, _ = create_case(conn, title='two', case_type='faq', severity='P3', confidence=.9)
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (first, second))
    assert preview(conn, first)['linked_cases'] == 1
    assert {'reason': 'linked_case_review_required', 'count': 1} in preview(conn, first)['observed_holds']
    assert preview(conn, second)['canonical_case_present']


@pytest.mark.parametrize('limit', [True, 0, 10001, '10'])
def test_inventory_rejects_invalid_bounds(conn, limit):
    with pytest.raises(ValueError):
        preview(conn, 'case', row_limit=limit)


def test_inventory_unknown_case_does_not_create_data(conn):
    before = conn.serialize()
    with pytest.raises(ValueError, match='not found'):
        preview(conn, 'absent')
    assert conn.serialize() == before


def test_schema_change_is_visible_without_guessing_column_semantics(conn):
    case, _ = create_case(conn, title='test', case_type='faq', severity='P3', confidence=.9)
    before = preview(conn, case)
    conn.execute('CREATE TABLE future_copy(id INTEGER, arbitrary_content TEXT)')
    conn.execute("INSERT INTO future_copy VALUES(1,'PRIVATE FUTURE COPY')")
    after = preview(conn, case)
    assert after['schema_digest'] != before['schema_digest']
    assert after['unclassified_columns'] == before['unclassified_columns'] + 2
    assert not after['coverage_complete'] and not after['deletion_allowed']
    assert 'PRIVATE FUTURE COPY' not in str(after)


@pytest.mark.parametrize('state,held', [('pending',True),('sending',True),('retry',True),
    ('permanent_failure',True),('delivered',False),('cancelled',False)])
def test_delivery_hold_distinguishes_unsettled_from_terminal(conn, state, held):
    case, _ = create_case(conn, title='test', case_type='faq', severity='P3', confidence=.9)
    conn.execute('''INSERT INTO outbox(outbox_id,channel,action_type,destination,payload_json,
        idempotency_key,state,case_id,created_at,updated_at)
        VALUES('inventory-send','telegram','reply','private','{}','inventory-key',?,?, 'now','now')''', (state,case))
    result = preview(conn, case)
    assert any(h['reason'] == 'delivery_unsettled' for h in result['observed_holds']) == held
    assert not result['deletion_allowed']
    conn.execute("UPDATE outbox SET lease_owner='stale-worker' WHERE outbox_id='inventory-send'")
    assert any(h['reason'] == 'delivery_unsettled' for h in preview(conn, case)['observed_holds'])


def test_indirect_source_is_deduplicated_and_other_case_excluded(conn):
    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='message',
                            payload={'text': 'PRIVATE SOURCE'}, occurred_at='2026-01-01T00:00:00Z')
    case, _ = create_case(conn, title='one', case_type='faq', severity='P3', confidence=.9,
                          source_event_pk=event)
    other, _ = create_case(conn, title='two', case_type='faq', severity='P3', confidence=.9)
    conn.execute('''INSERT INTO case_sources(source_id,case_id,source_type,stable_external_id,
        visibility,requester_access) VALUES('source',?,'message','message','internal','unknown')''', (case,))
    before = conn.serialize()
    result = preview(conn, case)
    source = next(r for r in result['surfaces'] if r['table'] == 'inbound_events')
    assert source['rows_counted'] == 1 and source['bytes_counted'] > 0
    assert 'PRIVATE SOURCE' not in str(result)
    assert next(r for r in preview(conn, other)['surfaces'] if r['table'] == 'inbound_events')['rows_counted'] == 0
    assert conn.serialize() == before


@pytest.mark.parametrize('reference', ['case_event', 'external_source', 'context_focus', 'unbound_context_focus'])
def test_shared_message_hold_counts_messages_not_references(conn, reference):
    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='shared',
        payload={'text':'secret'}, occurred_at='2026-01-01T00:00:00Z')
    first, _ = create_case(conn, title='first', case_type='faq', severity='P3', confidence=.9, source_event_pk=event)
    assert not any(h['reason']=='shared_source_messages' for h in preview(conn, first)['observed_holds'])
    for n in range(2):
        second, _ = create_case(conn, title=f'other {n}', case_type='faq', severity='P3', confidence=.9,
            source_event_pk=event if reference=='case_event' else None)
        if reference=='external_source':
            conn.execute('''INSERT INTO case_sources(source_id,case_id,source_type,stable_external_id,
                visibility,requester_access) VALUES(?,?,'message','shared','internal','unknown')''', (f'shared-{n}',second))
        if reference in {'context_focus','unbound_context_focus'}:
            conn.execute('''INSERT INTO conversation_contexts(context_id,chat_id,chat_type,case_id,
                focus_event_pk,input_digest,created_at,updated_at) VALUES(?,?,'p2p',?,?,'d','now','now')''',
                (f'shared-context-{n}',f'chat-{n}',second if reference=='context_focus' else None,event))
    result = preview(conn, first)
    assert {'reason':'shared_source_messages','count':1} in result['observed_holds']
    root, _ = create_case(conn,title='root',case_type='faq',severity='P3',confidence=.9)
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (root,first))
    group = preview(conn,root)['canonical_group_holds']['members']
    member = next(row for row in group if row['case_id']==first)
    assert {'reason':'shared_source_messages','count':1} in member['dependency_holds']
    assert 'secret' not in str(result)


@pytest.mark.parametrize('relation', ['member','alias'])
@pytest.mark.parametrize('bound', [False,True])
def test_context_member_and_alias_shared_sources_are_retained(conn, relation, bound):
    event, _ = ingest_event(conn,source='feishu_user_poll',identity='user',external_id='indirect-shared',
                           payload={'text':'PRIVATE'},occurred_at='2026-01-01T00:00:00Z')
    case, _ = create_case(conn,title='source',case_type='faq',severity='P3',confidence=.9,source_event_pk=event)
    other, _ = create_case(conn,title='other',case_type='faq',severity='P3',confidence=.9)
    conn.execute('''INSERT INTO conversation_contexts(context_id,chat_id,chat_type,case_id,input_digest,created_at,updated_at)
        VALUES('indirect-context','chat','p2p',?,'digest','now','now')''', (other if bound else None,))
    if relation=='member':
        conn.execute("INSERT INTO conversation_context_members VALUES(?,'indirect-context',1,'d','c','colleague','same','now')",(event,))
    else:
        conn.execute("INSERT INTO conversation_anchor_aliases VALUES('chat','anchor','indirect-context',?)",(event,))
    before = conn.serialize()
    result = preview(conn,case)
    assert {'reason':'shared_source_messages','count':1} in result['observed_holds']
    assert 'PRIVATE' not in str(result) and conn.serialize()==before


@pytest.mark.parametrize('reference', ['canonical', 'event', 'external', 'document'])
@pytest.mark.parametrize('status', ['approved', 'retired'])
def test_knowledge_provenance_holds_even_retired_entries(conn, reference, status):
    from test_knowledge_feedback import approved_knowledge
    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='knowledge-message',
        payload={'text':'private evidence'}, occurred_at='2026-01-01T00:00:00Z')
    case, _ = create_case(conn, title='case', case_type='faq', severity='P3', confidence=.9, source_event_pk=event)
    other, _ = create_case(conn, title='other', case_type='faq', severity='P3', confidence=.9)
    knowledge = approved_knowledge(conn, case if reference=='canonical' else other)
    conn.execute('UPDATE knowledge_entries SET status=? WHERE knowledge_id=?', (status,knowledge))
    if reference != 'canonical':
        source = {'event': event, 'external':'knowledge-message','document':'document-id'}[reference]
        conn.execute('''INSERT INTO knowledge_sources(mapping_id,knowledge_id,source_type,stable_external_id,
            visibility,claim) VALUES('knowledge-source',?,'document',?,'internal','private claim')''', (knowledge,source))
        if reference == 'document':
            conn.execute('''INSERT INTO case_sources(source_id,case_id,source_type,stable_external_id,
                visibility,requester_access) VALUES('doc-source',?,'document','document-id','internal','unknown')''', (case,))
    before = conn.serialize()
    result = preview(conn, case)
    assert {'reason':'knowledge_source_reference','count':1} in result['observed_holds']
    assert 'private claim' not in str(result) and 'private evidence' not in str(result)
    assert conn.serialize() == before
