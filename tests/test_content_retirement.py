import pytest
import sqlite3

from test_conversation_context import admitted, case, item
from k3_support import conversation_context as context
from k3_support.content_retirement import ContentRetiredError, require_case_content
from k3_support.case_detail import case_detail
from k3_support.case_actions import action_binding
from k3_support.delivery import _content_eligibility


@pytest.mark.parametrize('marker', ['complete','schema_only','state_only'])
def test_tombstone_without_receipt_cannot_reconstruct_from_old_sources(conn,config,marker):
    from k3_support.retention_fact_fields import fact_fields, redact_fact_content
    from k3_support.ids import canonical_json,digest
    key,snapshot = admitted(conn,config,item(content='当前使用 UFS'))
    cid = case(conn,key)
    context.bind_context_case(conn,snapshot['context_id'],cid)
    snapshot = context.context_snapshot(conn,snapshot['context_id'])
    classified = fact_fields(snapshot['facts'])
    tombstone = redact_fact_content(snapshot['facts'],expected_digest=classified['input_digest'])['value']
    if marker=='schema_only':
        tombstone.pop('content_state')
    elif marker=='state_only':
        tombstone.pop('schema')
    conn.execute('UPDATE conversation_contexts SET facts_json=?,facts_digest=? WHERE context_id=?',
                 (canonical_json(tombstone),digest(tombstone),snapshot['context_id']))
    assert conn.execute('SELECT count(*) FROM case_content_retirements').fetchone()[0]==0
    before = conn.serialize()
    def no_source_read(action,table,column,*_):
        if action==sqlite3.SQLITE_READ and (
            (table=='inbound_events' and column=='payload_json')
            or (table=='conversation_contexts' and column=='query_text')):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(no_source_read)
    try:
        for call in (context.context_snapshot,context.project_context):
            with pytest.raises(ContentRetiredError,match='清理标记'):
                call(conn,snapshot['context_id'])
        assert context.validate_context_binding(conn,snapshot['binding']) == (False,'context_content_retired')
    finally:
        conn.set_authorizer(None)
    assert conn.serialize()==before


def test_retired_detail_lifecycle_buttons_use_authenticated_current_tokens(conn, config):
    from k3_support.control import ControlMessage, execute_control
    key, _ = admitted(conn,config,item())
    cid = case(conn,key)
    conn.execute("UPDATE cases SET state='resolved' WHERE case_id=?", (cid,))
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid,1,'lifecycle-retired','a'*64,'b'*64,'now','fixture'))
    preview = case_detail(conn,case_id=cid)['preview']
    reopen = next(button for button in preview['buttons'] if button['text']=='重新打开（人工负责）')
    token = reopen['callback_data'].rsplit(':',1)[1]
    command = f'case-action reopen {cid} {token}'
    before = conn.serialize()
    with pytest.raises(Exception,match='identity'):
        execute_control(conn,config,ControlMessage('stranger','owner-chat','no-auth',command))
    assert conn.serialize() == before
    result = execute_control(conn,config,ControlMessage('owner-user','owner-chat','reopen-owner',command))
    assert conn.execute('SELECT lifecycle_round FROM cases WHERE case_id=?',(cid,)).fetchone()[0] == 2
    resolve = next(button for button in result['preview']['buttons'] if button['text']=='标记解决')
    assert not any(button['text']=='交给 AI' for button in result['preview']['buttons'])
    with pytest.raises(ValueError,match='stale'):
        execute_control(conn,config,ControlMessage('owner-user','owner-chat','old-token',command))
    resolve_token = resolve['callback_data'].rsplit(':',1)[1]
    closed = execute_control(conn,config,ControlMessage('owner-user','owner-chat','resolve-owner',
        f'case-action resolve {cid} {resolve_token}'))
    assert conn.execute('SELECT state FROM cases WHERE case_id=?',(cid,)).fetchone()[0] == 'resolved'
    assert any(button['text']=='重新打开（人工负责）' for button in closed['preview']['buttons'])
    assert 'Pico 风扇怎么调' not in closed['preview']['plain_text']


@pytest.mark.parametrize('action', ['claim','delegate','suggest_only'])
@pytest.mark.parametrize('current_decision', [False,True])
def test_retired_turn_cannot_be_reactivated_after_reopen(conn, config, action, current_decision):
    from k3_support.coordination import ensure_turn, control_communication, CoordinationError
    from k3_support.lifecycle import operator_transition
    key, snapshot = admitted(conn, config, item())
    cid = case(conn,key)
    context.bind_context_case(conn,snapshot['context_id'],cid)
    ensure_turn(conn,case_id=cid,source_event_pk=key)
    old = action_binding(conn,case_id=cid,action=action)
    conn.execute("UPDATE cases SET state='resolved' WHERE case_id=?", (cid,))
    version = conn.execute('SELECT version FROM cases WHERE case_id=?', (cid,)).fetchone()[0]
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid,1,'turn-retired','a'*64,'b'*64,'now','fixture'))
    operator_transition(conn,case_id=cid,action='reopen',expected_version=version,
                        actor_id='fixture',idempotency_key='turn-reopen')
    before = conn.serialize()
    with pytest.raises(ContentRetiredError,match='旧轮次对话'):
        action_binding(conn,case_id=cid,action=action)
    with pytest.raises(CoordinationError,match='旧轮次对话'):
        control_communication(conn,case_id=cid,action=action,actor_id='fixture',
                              external_id='forbidden-old-turn',turn_id=old['turn_id'])
    assert conn.serialize()==before
    new_key,new_context = admitted(conn,config,item(2,content='新问题',at=True))
    context.bind_context_case(conn,new_context['context_id'],cid)
    ensure_turn(conn,case_id=cid,source_event_pk=new_key)
    fresh = action_binding(conn,case_id=cid,action=action)
    assert fresh['turn_id'] != old['turn_id'] and fresh['token'] != old['token']
    shown = case_detail(conn,case_id=cid)['preview']
    controls = {button['text']:button['callback_data'] for button in shown['buttons']}
    assert {'我来回复','只给我建议','交给 AI'} <= controls.keys()
    assert all(len(value.encode()) <= 64 for value in controls.values())
    assert all(old['token'] not in value for value in controls.values())
    from k3_support.control import execute_control, ControlMessage
    from test_coordination import active_config
    cfg = active_config(config)
    conn.executemany('''INSERT INTO route_decisions(route_decision_id,event_pk,case_id,route,proposed_route,
        confidence,issue_type,severity,domain,requires_owner_judgment,profile_snapshot_json,
        model_output_digest,created_at) VALUES(?,?,?,'owner_decision','owner_decision',
        .9,'faq','P3','bootloader',1,'{}','fixture','now')''',
        [('old-decision',key,cid)] + ([('new-decision',new_key,cid)] if current_decision else []))
    result = execute_control(conn,cfg,ControlMessage('owner-user','owner-chat','allowed-new-turn',
        f"case-action {action} {cid} {fresh['token']}"))
    assert result['turn']['turn_id'] == fresh['turn_id']
    assert conn.execute("SELECT route FROM route_decisions WHERE route_decision_id='old-decision'").fetchone()[0] == 'owner_decision'
    if current_decision:
        expected_route = 'codex_debug' if action == 'delegate' else 'owner_decision'
        assert conn.execute("SELECT route FROM route_decisions WHERE route_decision_id='new-decision'").fetchone()[0] == expected_route
    if action == 'delegate':
        assert result['continuation']['created'] is True
        from k3_support.retrieval import retrieval_input_for_case
        source = retrieval_input_for_case(conn,case_id=cid)
        assert source['source_event_pk'] == new_key
        assert '新问题' in source['full_query'] and 'Pico 风扇怎么调' not in source['full_query']
    assert action_binding(conn,case_id=cid,action=action)['token'] != fresh['token']
    with pytest.raises(ValueError,match='stale'):
        case_detail(conn,case_id=cid,expected_digest=shown['content_digest'])


def test_reopened_status_without_context_excludes_old_round_and_payloads(conn, config):
    from k3_support.store import enqueue_outbox
    key, _ = admitted(conn, config, item())
    cid = case(conn, key)
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid,1,'status-retired','a'*64,'b'*64,'now','fixture'))
    conn.execute('UPDATE cases SET lifecycle_round=2 WHERE case_id=?', (cid,))
    for suffix in ('old', 'new'):
        conn.execute('''INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,
            available_at,created_at,updated_at,error_class) VALUES(?,?,'codex','failed',?,
            'now','now','now','PRIVATE ERROR')''', ('job-'+suffix,cid,suffix))
        conn.execute('''INSERT INTO approvals(approval_id,case_id,approval_type,status,
            requested_action_json,action_digest,requested_at,expires_at,created_at,updated_at)
            VALUES(?,?,'wip_push','denied','{"text":"PRIVATE APPROVAL"}',?,
            'now','later','now','now')''', ('approval-'+suffix,cid,suffix))
        oid, _ = enqueue_outbox(conn,channel='feishu_im',action_type='reply',destination='fixture',
                               payload={'text':'PRIVATE REPLY'},idempotency_key=suffix,case_id=cid)
        if suffix == 'old':
            for table, column, value in (('jobs','job_id','job-old'),
                                         ('approvals','approval_id','approval-old'),
                                         ('outbox','outbox_id',oid)):
                conn.execute(f'UPDATE {table} SET lifecycle_round=1 WHERE {column}=?', (value,))
    before = conn.serialize()
    def no_body(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and column in {
            'error_class','payload_json','requested_action_json','decision_text','title','next_action'}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(no_body)
    try:
        shown = case_detail(conn,case_id=cid)['preview']
    finally:
        conn.set_authorizer(None)
    assert '本轮尚无新上下文' in shown['plain_text']
    assert 'job-new' in shown['plain_text'] and 'approval-new' in shown['plain_text']
    assert 'job-old' not in shown['plain_text'] and 'approval-old' not in shown['plain_text']
    assert 'PRIVATE' not in shown['plain_text']
    assert conn.serialize() == before
    conn.execute("UPDATE approvals SET status='expired' WHERE approval_id='approval-new'")
    with pytest.raises(ValueError,match='stale'):
        case_detail(conn,case_id=cid,expected_digest=shown['content_digest'])


def test_context_retirement_check_and_content_read_share_transaction(conn, config, monkeypatch):
    key, snapshot = admitted(conn, config, item())
    cid = case(conn,key)
    context.bind_context_case(conn,snapshot['context_id'],cid)
    from k3_support import content_retirement
    original = content_retirement.require_case_content
    observed = []
    def checked(*args, **kwargs):
        observed.append(conn.in_transaction)
        return original(*args, **kwargs)
    monkeypatch.setattr(content_retirement,'require_case_content',checked)
    assert not conn.in_transaction
    context.context_snapshot(conn,snapshot['context_id'])
    assert observed == [True] and not conn.in_transaction


def test_current_round_status_reports_truncated_history(conn, config):
    key, _ = admitted(conn, config, item())
    cid = case(conn, key)
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid,1,'limited-retired','a'*64,'b'*64,'now','fixture'))
    conn.execute('UPDATE cases SET lifecycle_round=2 WHERE case_id=?', (cid,))
    for index in range(21):
        conn.execute('''INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,
            available_at,created_at,updated_at) VALUES(?,?,'codex','failed',?,'now','now','now')''',
            (f'bounded-{index:02}',cid,str(index)))
    shown = case_detail(conn,case_id=cid)['preview']
    assert '任务仅显示最近 20 条创建记录' in shown['plain_text']
    assert 'bounded-00' not in shown['plain_text']
    assert 'bounded-20' in shown['plain_text']
    assert shown['plain_text'].count('任务 bounded-') == 20


def test_context_snapshot_is_consistent_across_concurrent_retirement(conn, config, monkeypatch):
    from k3_support import content_retirement
    key, snapshot = admitted(conn,config,item())
    cid = case(conn,key)
    context.bind_context_case(conn,snapshot['context_id'],cid)
    original = content_retirement.require_case_content
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    assert conn.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    writer = sqlite3.connect(path,isolation_level=None,timeout=1)
    wrote = []
    def retire_after_check(*args, **kwargs):
        original(*args, **kwargs)
        if not wrote:
            writer.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                           (cid,1,'concurrent-retirement','a'*64,'b'*64,'now','fixture'))
            wrote.append(True)
    monkeypatch.setattr(content_retirement,'require_case_content',retire_after_check)
    try:
        result = context.context_snapshot(conn,snapshot['context_id'])
        assert result['case_id']==cid and wrote==[True]
        with pytest.raises(ContentRetiredError):
            context.context_snapshot(conn,snapshot['context_id'])
    finally:
        writer.close()


def test_retired_round_cannot_create_new_coding_job_or_files(conn, config):
    from test_executors import executor_config
    from k3_support.executors import create_codex_job, ExecutorError
    from k3_support.store import create_case
    cfg = executor_config(config,codex=True)
    cid, _ = create_case(conn,title='fixture',case_type='bug',severity='P2',confidence=.5)
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid,1,'create-retired','a'*64,'b'*64,'now','fixture'))
    before = conn.serialize()
    paths = set(cfg.data_dir.rglob('*'))
    with pytest.raises(ExecutorError,match='content retired'):
        create_codex_job(conn,cfg,case_id=cid,
            brief='# UNTRUSTED INPUT\nfixture\n# FORBIDDEN ACTIONS\nNo push\n# ACCEPTANCE TESTS\nBuild',repo='u-boot')
    assert conn.serialize()==before and set(cfg.data_dir.rglob('*'))==paths


@pytest.mark.parametrize('round_number', [True, False, 0, -1, '1', 1.0, [], {}])
def test_retirement_guard_rejects_ambiguous_rounds(conn, round_number):
    with pytest.raises(ValueError, match='lifecycle round'):
        require_case_content(conn, case_id='fixture', lifecycle_round=round_number)


@pytest.mark.parametrize('identity', ['', False, 1, [], {}, 'x'*129])
def test_retirement_guard_rejects_invalid_case_identity(conn, identity):
    with pytest.raises(ValueError, match='Case identity'):
        require_case_content(conn, case_id=identity, lifecycle_round=1)


def test_workbench_masks_retired_case_content(conn, config):
    from k3_support.workbench import _queue_query, _queue_params
    from datetime import datetime, UTC
    key, _ = admitted(conn, config, item())
    cid = case(conn, key)
    conn.execute("UPDATE cases SET title='PRIVATE TITLE',next_action='PRIVATE ACTION' WHERE case_id=?", (cid,))
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid, 1, 'queue-retired', 'a'*64, 'b'*64, 'now', 'fixture'))
    before = conn.serialize()
    rows = conn.execute(_queue_query(conn) + "SELECT title,next_action FROM queue WHERE item_id=:item_id",
                        {**_queue_params(config, datetime.now(UTC)), 'item_id': 'case:'+cid}).fetchall()
    assert len(rows) == 1 and rows[0]['title'] == '历史内容已清理'
    assert 'PRIVATE' not in str([dict(row) for row in rows])
    assert '旧内容不可恢复' in rows[0]['next_action']
    assert conn.serialize() == before


def test_workbench_failed_delivery_does_not_return_retired_payload_as_revision(conn, config):
    from k3_support.workbench import _queue_query, _queue_params
    from k3_support.store import enqueue_outbox
    from datetime import datetime, UTC
    key, _ = admitted(conn, config, item())
    cid = case(conn, key)
    outbox, _ = enqueue_outbox(conn, channel='feishu_im', action_type='reply', destination='fixture',
                              payload={'text':'PRIVATE FAILED REPLY'}, idempotency_key='retired-failure', case_id=cid)
    conn.execute("UPDATE outbox SET state='permanent_failure' WHERE outbox_id=?", (outbox,))
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid, 1, 'failed-retired', 'a'*64, 'b'*64, 'now', 'fixture'))
    row = conn.execute(_queue_query(conn) + 'SELECT * FROM queue WHERE item_id=:item_id',
                       {**_queue_params(config, datetime.now(UTC)), 'item_id':'outbox:'+outbox}).fetchone()
    assert row['kind'] == 'delivery_failure'
    assert row['revision'].startswith('retired:')
    assert 'PRIVATE' not in str(dict(row))


def test_workbench_retired_job_keeps_failure_without_diagnostic_text(conn, config):
    from k3_support.workbench import _queue_query, _queue_params
    from datetime import datetime, UTC
    key, _ = admitted(conn, config, item())
    cid = case(conn, key)
    conn.execute('''INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,created_at,updated_at,error_class)
        VALUES('retired-job',?,'codex','failed','retired-job','now','now','now','PRIVATE DIAGNOSTIC')''', (cid,))
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid, 1, 'job-retired', 'a'*64, 'b'*64, 'now', 'fixture'))
    row = conn.execute(_queue_query(conn) + "SELECT * FROM queue WHERE item_id='job:retired-job'",
                       _queue_params(config, datetime.now(UTC))).fetchone()
    assert row['kind'] == 'job_failure'
    assert '历史内容已清理' in row['title']
    assert 'PRIVATE' not in str(dict(row))
    from k3_support.execution_inventory import page
    inventory = page(conn, config, state='failed')
    projected = next(item for item in inventory['items'] if item['job_id'] == 'retired-job')
    assert projected['error_class'] == 'case_content_retired'
    assert projected['content_retired'] and not projected['input_recovery_available']
    assert 'PRIVATE' not in str(projected)


@pytest.mark.parametrize('legacy', [False, True])
def test_retrieval_refuses_retired_material_before_context_or_event_read(conn, config, legacy):
    from k3_support.retrieval import retrieval_input_for_case, _legacy_input, RetrievalError
    key, snapshot = admitted(conn, config, item())
    cid = case(conn, key)
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid, 1, 'retrieval-retired', 'a'*64, 'b'*64, '2026-09-09', 'fixture'))
    def no_source(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and table in {'conversation_contexts', 'inbound_events'}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    before = conn.serialize()
    conn.set_authorizer(no_source)
    try:
        with pytest.raises(RetrievalError, match='retrieval_content_retired'):
            if legacy:
                _legacy_input(conn, case_id=cid, source_event_pk=key, query='old', lifecycle_round=1)
            else:
                retrieval_input_for_case(conn, case_id=cid, source_event_pk=key)
    finally:
        conn.set_authorizer(None)
    assert conn.serialize() == before


@pytest.mark.parametrize('entry', ['items', 'reviewed_versions', 'reviewed_version_scan', 'lines'])
def test_board_evidence_entry_points_reject_retired_round_before_reading(conn, config, entry):
    from k3_support import board_test_evidence
    key, snapshot = admitted(conn, config, item())
    cid = case(conn, key)
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid, 1, 'board-retired', 'a'*64, 'b'*64, '2026-09-09', 'fixture'))
    def no_evidence(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and table in {'codex_reviews', 'evidence', 'broker_board_actions', 'broker_board_results'}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    before = conn.serialize()
    conn.set_authorizer(no_evidence)
    try:
        with pytest.raises(ContentRetiredError):
            getattr(board_test_evidence, entry)(conn, case_id=cid, lifecycle_round=1)
    finally:
        conn.set_authorizer(None)
    assert conn.serialize() == before


def test_retired_round_cannot_rebuild_context_or_display_old_details(conn, config):
    key, snapshot = admitted(conn, config, item())
    cid = case(conn, key)
    context.bind_context_case(conn, snapshot['context_id'], cid)
    snapshot = context.resolve_event_context(conn, key)
    old_reopen = action_binding(conn, case_id=cid, action='reopen')['token']
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid, snapshot['lifecycle_round'], 'fixture-receipt', 'a'*64, 'b'*64, '2026-09-09', 'fixture'))
    before = conn.serialize()
    assert context.validate_context_binding(conn, snapshot['binding']) == (False, 'context_content_retired')
    assert _content_eligibility(conn, config, {'case_id':cid, 'lifecycle_round':snapshot['lifecycle_round']}, {}) == (False, 'case_content_retired')
    with pytest.raises(ContentRetiredError):
        action_binding(conn, case_id=cid, action='delegate')
    assert action_binding(conn, case_id=cid, action='reopen')['token'] != old_reopen
    with pytest.raises(ContentRetiredError):
        context.project_context(conn, snapshot['context_id'])
    def no_body(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and ((table == 'cases' and column in {'title','next_action'})
                or (table == 'conversation_contexts' and column in {'query_text','facts_json'})
                or (table == 'inbound_events' and column == 'payload_json')
                or table in {'evidence','case_sources'}):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(no_body)
    try:
        assert context.validate_context_binding(conn, snapshot['binding']) == (False, 'context_content_retired')
        with pytest.raises(ContentRetiredError):
            context.project_context(conn, snapshot['context_id'])
        with pytest.raises(ContentRetiredError):
            context.context_snapshot(conn, snapshot['context_id'])
        with pytest.raises(ContentRetiredError):
            context.resolve_event_context(conn, key)
        shown = case_detail(conn, case_id=cid)['preview']
    finally:
        conn.set_authorizer(None)
    assert shown['content_state'] == 'retired_metadata_only'
    assert 'fixture-receipt' in shown['plain_text']
    assert 'Synthetic case' not in shown['plain_text']
    assert [button['text'] for button in shown['buttons']] == ['返回工作台']
    with pytest.raises(ValueError, match='stale'):
        case_detail(conn, case_id=cid, expected_digest='0'*64)
    assert conn.serialize() == before
    # New-round material is not retired by an older receipt, while the aggregate
    # detail remains unavailable until it can omit retired rounds explicitly.
    require_case_content(conn, case_id=cid, lifecycle_round=snapshot['lifecycle_round']+1)


@pytest.mark.parametrize('new_text', ['新一轮独立问题 <tag>', '新一轮独立问题 <tag>' * 300], ids=['short','paged'])
def test_reopening_retired_case_does_not_restore_old_context(conn, config, new_text):
    from k3_support.lifecycle import operator_transition

    key, snapshot = admitted(conn, config, item())
    cid = case(conn, key)
    context.bind_context_case(conn, snapshot['context_id'], cid)
    snapshot = context.resolve_event_context(conn, key)
    conn.execute("UPDATE cases SET state='resolved' WHERE case_id=?", (cid,))
    version = conn.execute('SELECT version FROM cases WHERE case_id=?', (cid,)).fetchone()[0]
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid, snapshot['lifecycle_round'], 'reopen-receipt', 'a'*64, 'b'*64, '2026-09-09', 'fixture'))
    result = operator_transition(conn, case_id=cid, action='reopen', expected_version=version,
                                 actor_id='fixture', idempotency_key='retired-reopen')
    assert result['lifecycle_round'] == snapshot['lifecycle_round'] + 1
    assert not context.validate_context_binding(conn, snapshot['binding'])[0]
    with pytest.raises(ContentRetiredError):
        context.project_context(conn, snapshot['context_id'])
    # New independently admitted material is visible without reading aggregate
    # titles, old source messages, or evidence from earlier rounds.
    new_key, new_snapshot = admitted(conn, config, item(2, content=new_text, at=True))
    context.bind_context_case(conn, new_snapshot['context_id'], cid)
    before = conn.serialize()
    def no_aggregate_body(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and (
                (table == 'cases' and column in {'title', 'next_action'})
                or table in {'evidence', 'case_sources', 'case_events'}
                or (table == 'approvals' and column in {'requested_action_json','decision_text'})):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(no_aggregate_body)
    try:
        shown = case_detail(conn, case_id=cid)['preview']
        pages = [shown] + [case_detail(conn, case_id=cid, page=page,
                          expected_digest=shown['content_digest'])['preview']
                          for page in range(2, shown['page_count']+1)]
    finally:
        conn.set_authorizer(None)
    assert shown['content_state'] == 'current_context_with_retired_history'
    assert new_text in ''.join(page['plain_text'] for page in pages)
    assert '&lt;tag&gt;' in shown['text']
    assert 'Pico 风扇怎么调' not in shown['plain_text']
    assert all(not button['callback_data'].startswith(('wka2:c:','wka2:s:','wka2:a:'))
               for button in shown['buttons'])
    assert conn.serialize() == before
    for page in pages:
        assert len(page['text']) <= 2800
        assert all(len(button['callback_data'].encode()) <= 64 for button in page['buttons'])
    with pytest.raises(ValueError, match='invalid current-round'):
        case_detail(conn, case_id=cid, page=shown['page_count']+1)
    conn.execute("UPDATE inbound_events SET payload_json=json_set(payload_json,'$.content','changed') WHERE event_pk=?",
                 (new_key,))
    with pytest.raises(ValueError, match='stale'):
        case_detail(conn, case_id=cid, expected_digest=shown['content_digest'])
    assert '暂不展示内容' in case_detail(conn, case_id=cid)['preview']['plain_text']
