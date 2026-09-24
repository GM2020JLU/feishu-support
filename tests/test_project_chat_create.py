"""Shared authenticated chat admission; native schema is injected, never written."""
import json
import shlex
from datetime import timedelta

import pytest
from test_project_chat_control import message

from k3_support import project_bug_create, project_create_schema
from k3_support import project_create_grants as grants
from k3_support.approvals import ApprovalError
from k3_support.control import ControlMessage, execute_control
from k3_support.timeutil import utc_now


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_chat_issues_configured_create_grant_without_creating_bug(conn,config,channel):
    from test_project_refresh import READER

    config.raw['identity']['control_operator_id']='owner-user'
    config.raw['project_integration']={'reader':dict(READER),
        'intake_spaces':[{'simple_name':'k3','project_key':'fixture-space','type_keys':['issue']}]}
    options=execute_control(conn,config,message(config,'bug create-scope-options',channel,'scope-options'),control_channel=channel)
    assert 'fixture-space / issue' in options['text']
    expiry=(utc_now()+timedelta(hours=1)).isoformat()
    command=f'bug issue-create-grant fixture-space issue 1 {expiry}'
    msg=message(config,command,channel,'issue-create-grant')
    first=execute_control(conn,config,msg,control_channel=channel)
    grant=conn.execute('SELECT * FROM project_create_grants').fetchone()
    assert grant['host']=='project.feishu.cn' and grant['max_creations']==1
    assert grant['grant_id'] in first['text']
    grants.revoke(conn,grant_id=grant['grant_id'],actor='owner-user')
    assert grant['grant_id'] in execute_control(conn,config,msg,control_channel=channel)['text']
    assert conn.execute('SELECT count(*) FROM project_create_grants').fetchone()[0]==1
    with pytest.raises(ValueError,match='create grant intent'):
        execute_control(conn,config,ControlMessage(msg.user_id,msg.chat_id,msg.message_id,
                        f'bug issue-create-grant fixture-space issue 2 {expiry}'),control_channel=channel)
    assert conn.execute('SELECT count(*) FROM project_bug_create_drafts').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0]==0


def test_chat_create_grant_rejects_unconfigured_scope_and_foreign_identity(conn,config):
    from test_project_refresh import READER

    config.raw['identity']['control_operator_id']='owner-user'
    config.raw['project_integration']={'reader':dict(READER),
        'intake_spaces':[{'simple_name':'k3','project_key':'fixture-space','type_keys':['issue']}]}
    expiry=(utc_now()+timedelta(hours=1)).isoformat()
    bad=message(config,f'bug issue-create-grant other-space issue 1 {expiry}','telegram','bad-scope')
    with pytest.raises(PermissionError,match='创建范围'):
        execute_control(conn,config,bad,control_channel='telegram')
    valid=message(config,f'bug issue-create-grant fixture-space issue 1 {expiry}','telegram','foreign')
    with pytest.raises(ApprovalError):
        execute_control(conn,config,ControlMessage('foreign',valid.chat_id,valid.message_id,valid.text),control_channel='telegram')
    assert conn.execute('SELECT count(*) FROM project_create_grants').fetchone()[0]==0


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_chat_revokes_owned_create_grant_without_creating_bug(conn,creation,channel):
    cfg,grant,_=creation
    cmd=f"bug revoke-create-grant {grant['grant_id']}"
    msg=message(cfg,cmd,channel,'revoke-create-grant')
    first=execute_control(conn,cfg,msg,control_channel=channel)
    assert grant['grant_id'] in first['text']
    assert conn.execute('SELECT revoked_at FROM project_create_grants WHERE grant_id=?',
                        (grant['grant_id'],)).fetchone()[0]
    assert '已吊销' in execute_control(conn,cfg,msg,control_channel=channel)['text']
    assert conn.execute("SELECT count(*) FROM project_create_grant_events WHERE grant_id=? AND kind='revoked'",
                        (grant['grant_id'],)).fetchone()[0]==1
    with pytest.raises(ValueError,match='create grant revocation'):
        execute_control(conn,cfg,ControlMessage(msg.user_id,msg.chat_id,msg.message_id,
                        'bug revoke-create-grant another-grant'),control_channel=channel)
    assert conn.execute('SELECT count(*) FROM project_bug_create_drafts').fetchone()[0]==0


@pytest.fixture
def creation(conn, config, monkeypatch):
    config.raw['identity']['control_operator_id'] = 'owner-user'
    grant = grants.issue(conn, actor='owner-user', request_id='chat-create-scope',
                         scope={'host':'project.feishu.cn','project_key':'fixture-space',
                                'type_key':'fixture-type','max_creations':2},
                         expires_at=(utc_now()+timedelta(hours=1)).isoformat())
    calls=[]
    def form(*args, **kwargs):
        calls.append(kwargs)
        return {'scope':{k:kwargs[k] for k in ('host','project_key','type_key')},
                'fields':[{'field_key':'title','label':'标题','type':'text','required':True,'editor':'text'},
                          {'field_key':'severity','label':'严重程度','type':'select','required':True,
                           'editor':'select','options':[{'value':'actual-high','label':'高'}]}],
                'defaults_applied':False,'options_validated':False}
    monkeypatch.setattr(project_create_schema,'form',form)
    return config,grant,calls


def draft_command(grant, fields=None):
    return shlex.join(['bug','create-draft',grant['grant_id'],json.dumps(fields or {'title':'软件异常'},ensure_ascii=False)])


def note_command(grant, text):
    return shlex.join(['bug', 'create-note', grant['grant_id'], text])


def note_form(monkeypatch, calls, *, description_editor='text'):
    def form(*args, **kwargs):
        calls.append(kwargs)
        return {'scope': {k: kwargs[k] for k in ('host', 'project_key', 'type_key')},
                'fields': [
                    {'field_key': 'name', 'label': '标题', 'type': 'text',
                     'required': True, 'editor': 'text'},
                    {'field_key': 'description', 'label': '描述', 'type': 'multi_text',
                     'required': True, 'editor': description_editor},
                ], 'defaults_applied': False, 'options_validated': False}
    monkeypatch.setattr(project_create_schema, 'form', form)


def test_chat_create_note_uses_current_text_form_and_stays_local(conn, creation, monkeypatch):
    cfg, grant, calls = creation
    note_form(monkeypatch, calls)
    text = '\n  K3 启动异常  \n详情保留全文\n'
    response = execute_control(conn, cfg, message(cfg, note_command(grant, text), message_id='note'))
    row = conn.execute('SELECT * FROM project_bug_create_drafts').fetchone()
    assert row['draft_id'] in response['text'] and row['state'] == 'draft'
    assert json.loads(row['field_values_json']) == {'name': 'K3 启动异常', 'description': text}
    assert len(calls) == 1
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0] == 0


def test_chat_create_note_rejects_missing_or_nontext_official_fields(conn, creation, monkeypatch):
    cfg, grant, calls = creation
    note_form(monkeypatch, calls, description_editor='json')
    with pytest.raises(ValueError, match='not editable text'):
        execute_control(conn, cfg, message(cfg, note_command(grant, '标题\n描述'), message_id='bad-note'))
    assert conn.execute('SELECT count(*) FROM project_bug_create_drafts').fetchone()[0] == 0

    def missing(*args, **kwargs):
        return {'scope': {}, 'fields': [{'field_key': 'name', 'label': '标题', 'type': 'text',
                                         'required': True, 'editor': 'text'}]}
    monkeypatch.setattr(project_create_schema, 'form', missing)
    with pytest.raises(ValueError, match='lacks note fields'):
        execute_control(conn, cfg, message(cfg, note_command(grant, '标题\n描述'), message_id='missing-note'))


def test_chat_create_note_replay_is_exact_native_intent(conn, creation, monkeypatch):
    cfg, grant, calls = creation
    note_form(monkeypatch, calls)
    msg = message(cfg, note_command(grant, '标题\n描述'), message_id='note-replay')
    first = execute_control(conn, cfg, msg)
    replay = execute_control(conn, cfg, msg)
    draft_id = conn.execute('SELECT draft_id FROM project_bug_create_drafts').fetchone()[0]
    assert draft_id in first['text'] and draft_id in replay['text']
    same_values = draft_command(grant, {'name': '标题', 'description': '标题\n描述'})
    with pytest.raises(ValueError, match='different create intent'):
        execute_control(conn, cfg, ControlMessage(msg.user_id, msg.chat_id, msg.message_id, same_values))
    with pytest.raises(ValueError, match='different create intent'):
        execute_control(conn, cfg, ControlMessage(msg.user_id, msg.chat_id, msg.message_id,
                        note_command(grant, '标题\n已编辑描述')))
    assert conn.execute('SELECT count(*) FROM project_bug_create_drafts').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0] == 0


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_description_to_shared_draft_replay_and_changed_message_rejected(conn,creation,channel):
    cfg,grant,calls=creation
    msg=message(cfg,draft_command(grant),channel)
    response=execute_control(conn,cfg,msg,control_channel=channel)
    row=conn.execute('SELECT * FROM project_bug_create_drafts').fetchone()
    assert row['draft_id'] in response['text'] and row['state']=='draft'
    assert json.loads(row['field_values_json'])=={'title':'软件异常'}
    assert json.loads(row['missing_required_json'])==['severity']
    assert len(calls)==1 and calls[0]['project_key']=='fixture-space'
    # Replay must return original custody even if the schema or grant later changes.
    grants.revoke(conn,grant_id=grant['grant_id'],actor='owner-user')
    assert row['draft_id'] in execute_control(conn,cfg,msg,control_channel=channel)['text']
    assert len(calls)==1
    for changed in [draft_command(grant,{'title':'更换意图'}),'bug import https://project.feishu.cn/space/issue/detail/123']:
        with pytest.raises(ValueError):
            execute_control(conn,cfg,ControlMessage(msg.user_id,msg.chat_id,msg.message_id,changed),control_channel=channel)
    assert conn.execute('SELECT count(*) FROM project_bug_create_drafts').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM project_link_intakes').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0]==0


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_bad_identity_and_revoked_scope_rejected_before_metadata(conn,creation,channel):
    cfg,grant,calls=creation;msg=message(cfg,draft_command(grant),channel)
    with pytest.raises(ApprovalError):
        execute_control(conn,cfg,ControlMessage('intruder',msg.chat_id,msg.message_id,msg.text),control_channel=channel)
    grants.revoke(conn,grant_id=grant['grant_id'],actor='owner-user')
    with pytest.raises((ValueError,PermissionError)):
        execute_control(conn,cfg,msg,control_channel=channel)
    assert not calls
    assert conn.execute('SELECT count(*) FROM project_bug_create_drafts').fetchone()[0]==0


def test_schema_failure_never_leaves_partial_draft(conn,creation,monkeypatch):
    cfg,grant,_=creation
    def unavailable(*args,**kwargs):raise ValueError('metadata unavailable')
    monkeypatch.setattr(project_create_schema,'form',unavailable)
    with pytest.raises(ValueError):execute_control(conn,cfg,message(cfg,draft_command(grant)))
    assert conn.execute('SELECT count(*) FROM project_bug_create_drafts').fetchone()[0]==0


@pytest.mark.parametrize('raw',['{"title":"a","title":"b"}','{"title":NaN}','[]'])
def test_ambiguous_json_is_rejected_before_reading_schema(conn,creation,raw):
    cfg,grant,calls=creation
    cmd=shlex.join(['bug','create-draft',grant['grant_id'],raw])
    with pytest.raises(ValueError):execute_control(conn,cfg,message(cfg,cmd))
    assert not calls


def test_draft_detail_is_owned_and_long_fields_are_not_presented_as_complete(conn,creation):
    cfg,grant,_=creation
    execute_control(conn,cfg,message(cfg,draft_command(grant,{'title':'x'*3500})))
    row=conn.execute('SELECT draft_id FROM project_bug_create_drafts').fetchone()
    result=execute_control(conn,cfg,message(cfg,'bug draft '+row[0],message_id='detail'))
    assert len(result['text'].encode('utf-16-le'))//2<=3000
    assert '网页' in result['text'] and 'x'*3500 not in result['text']
    cfg.raw['identity']['control_operator_id']='another-owner'
    with pytest.raises(ValueError):execute_control(conn,cfg,message(cfg,'bug draft '+row[0],message_id='foreign'))

@pytest.mark.parametrize('state,item_id,expected',[
    ('created','12345','不要重复创建'),('unknown',None,'不要重发'),
])
def test_existing_created_or_unknown_draft_never_suggests_new_creation(conn,creation,state,item_id,expected):
    cfg,grant,_=creation
    msg=message(cfg,draft_command(grant))
    execute_control(conn,cfg,msg)
    # Inject remote-outcome states for presentation, without claiming native I/O.
    conn.execute('UPDATE project_bug_create_drafts SET state=?,created_item_id=?',(state,item_id))
    response=execute_control(conn,cfg,msg)
    assert expected in response['text']
    assert '尚未创建远端缺陷' not in response['text']
    assert conn.execute('SELECT count(*) FROM project_bug_create_drafts').fetchone()[0]==1


def test_concurrent_delivery_with_changed_metadata_reuses_committed_intent(conn,creation,monkeypatch):
    from k3_support import project_bug_create
    from k3_support.ids import digest
    cfg,grant,_=creation;msg=message(cfg,draft_command(grant))
    request_id='chat-'+digest({'channel':'telegram','chat':msg.chat_id,'message':msg.message_id})
    original=project_create_schema.form
    def concurrent(*args,**kwargs):
        # Another delivery committed the same intent with earlier field labels.
        project_bug_create.prepare(conn,actor='owner-user',request_id=request_id,
            grant_id=grant['grant_id'],host='project.feishu.cn',project_key='fixture-space',
            type_key='fixture-type',field_values={'title':'软件异常'},required_fields=[])
        return original(*args,**kwargs)
    monkeypatch.setattr(project_create_schema,'form',concurrent)
    result=execute_control(conn,cfg,msg)
    row=conn.execute('SELECT draft_id FROM project_bug_create_drafts').fetchone()
    assert row['draft_id'] in result['text']
    assert conn.execute('SELECT count(*) FROM project_bug_create_drafts').fetchone()[0]==1

@pytest.mark.parametrize('first',['draft','import'])
def test_import_and_draft_cannot_claim_the_same_native_request(conn,config,first):
    from test_project_link_intake import context as intake_context

    from k3_support import project_bug_create, project_link_intake
    context=intake_context.__wrapped__(conn,config)
    request_id='chat-cross-command'
    grant=grants.issue(conn,actor='owner',request_id='cross-grant',
        scope={'host':'project.feishu.cn','project_key':'space','type_key':'issue','max_creations':1},
        expires_at=(utc_now()+timedelta(hours=1)).isoformat())
    def draft():
        return project_bug_create.prepare(conn,actor='owner',request_id=request_id,
            grant_id=grant['grant_id'],host='project.feishu.cn',project_key='space',type_key='issue',
            field_values={'title':'draft'},required_fields=[])
    def imported():return project_link_intake.enqueue(conn,config,**(context|{'request_id':request_id}))
    one,two=(draft,imported) if first=='draft' else (imported,draft)
    one()
    with pytest.raises(ValueError,match='native message ID'):two()
    assert conn.execute('SELECT count(*) FROM project_bug_create_drafts').fetchone()[0]+conn.execute('SELECT count(*) FROM project_link_intakes').fetchone()[0]==1


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_chat_create_duplicate_confirmation_ready_and_replay_are_draft_bound(conn, creation, monkeypatch, channel):
    cfg, grant, _ = creation
    create = execute_control(conn, cfg, message(cfg, draft_command(
        grant, {'title': '软件异常', 'severity': {'value': 'actual-high'}}), channel), control_channel=channel)
    draft_id = conn.execute('SELECT draft_id FROM project_bug_create_drafts').fetchone()[0]
    digest = conn.execute('SELECT request_digest FROM project_bug_create_drafts').fetchone()[0]
    calls = []
    from k3_support import project_bug_controls
    original = project_bug_controls.execute

    def controlled(conn_, config_, *, action, payload):
        calls.append((action, payload))
        if action == 'search-create-duplicates':
            return {'search_id': 'search-chat', 'state': 'queued'}
        if action == 'attach-create-search':
            return project_bug_create.projection(project_bug_create.attach_duplicates(
                conn_, actor='owner-user', draft_id=draft_id, search_id='search-chat', candidates=[]))
        return original(conn_, config_, action=action, payload=payload)

    monkeypatch.setattr(project_bug_controls, 'execute', controlled)
    search = execute_control(conn, cfg, message(
        cfg, f'bug create-search {draft_id} {digest} boot', channel, 'search'), control_channel=channel)
    assert 'search-chat' in search['text']
    attached = execute_control(conn, cfg, message(
        cfg, f'bug create-attach {draft_id} {digest} search-chat', channel, 'attach'), control_channel=channel)
    assert '重复搜索结果已附加' in attached['text']
    confirmed = execute_control(conn, cfg, message(
        cfg, f'bug create-confirm {draft_id} {digest}', channel, 'confirm'), control_channel=channel)
    assert '非重复确认' in confirmed['text']
    ready = execute_control(conn, cfg, message(
        cfg, f'bug create-ready {draft_id} {digest}', channel, 'ready'), control_channel=channel)
    assert '草稿已就绪' in ready['text']
    # Redelivering the original attach after confirmation must not clear it.
    replay = execute_control(conn, cfg, message(
        cfg, f'bug create-attach {draft_id} {digest} search-chat', channel, 'attach'), control_channel=channel)
    row = conn.execute('SELECT state,duplicate_confirmed_at FROM project_bug_create_drafts').fetchone()
    assert row['state'] == 'ready' and row['duplicate_confirmed_at'] is not None
    assert '未重置' in replay['text']
    assert [call[0] for call in calls].count('attach-create-search') == 1
    assert create['command'] == 'project_bug'


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_chat_create_search_timeout_and_digest_guard_leave_draft_unchanged(conn, creation, monkeypatch, channel):
    cfg, grant, _ = creation
    execute_control(conn, cfg, message(cfg, draft_command(grant, {'title': '软件异常', 'severity': {'value': 'actual-high'}}), channel), control_channel=channel)
    draft_id, digest = conn.execute('SELECT draft_id,request_digest FROM project_bug_create_drafts').fetchone()
    from k3_support import project_bug_controls

    def expired(*_args, **_kwargs):
        raise ValueError('duplicate search is incomplete, expired or outside current scope')

    monkeypatch.setattr(project_bug_controls, 'execute', expired)
    with pytest.raises(ValueError, match='expired'):
        execute_control(conn, cfg, message(
            cfg, f'bug create-attach {draft_id} {digest} expired-search', channel, 'expired'), control_channel=channel)
    with pytest.raises(Exception, match='refresh'):
        execute_control(conn, cfg, message(
            cfg, f'bug create-confirm {draft_id} wrong-digest', channel, 'wrong'), control_channel=channel)
    row = conn.execute('SELECT state,duplicate_search_id,duplicate_confirmed_at FROM project_bug_create_drafts').fetchone()
    assert tuple(row) == ('draft', None, None)


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_chat_create_mutation_message_cannot_be_edited_to_another_draft(conn, creation, monkeypatch, channel):
    cfg, grant, _ = creation
    for message_id, title in [('first-draft', '一'), ('second-draft', '二')]:
        execute_control(conn, cfg, message(
            cfg, draft_command(grant, {'title': title, 'severity': {'value': 'actual-high'}}),
            channel, message_id), control_channel=channel)
    rows = conn.execute('SELECT draft_id,request_digest FROM project_bug_create_drafts ORDER BY rowid').fetchall()
    from k3_support import project_bug_controls
    calls = []

    def search(*_args, **kwargs):
        calls.append(kwargs['payload'])
        return {'search_id': 'search-edit', 'state': 'queued'}

    monkeypatch.setattr(project_bug_controls, 'execute', search)
    first = f"bug create-search {rows[0]['draft_id']} {rows[0]['request_digest']} boot"
    execute_control(conn, cfg, message(cfg, first, channel, 'mutable-message'), control_channel=channel)
    changed = f"bug create-search {rows[1]['draft_id']} {rows[1]['request_digest']} boot"
    with pytest.raises(ValueError, match='different create intent'):
        execute_control(conn, cfg, message(cfg, changed, channel, 'mutable-message'), control_channel=channel)
    assert len(calls) == 1


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_chat_create_bind_queues_only_the_created_draft_readback(conn, creation, monkeypatch, channel):
    cfg, grant, _ = creation
    execute_control(conn, cfg, message(
        cfg, draft_command(grant, {'title': '软件异常', 'severity': {'value': 'actual-high'}}), channel), control_channel=channel)
    draft_id, expected_digest = conn.execute(
        'SELECT draft_id,request_digest FROM project_bug_create_drafts').fetchone()
    project_bug_create.attach_duplicates(conn, actor='owner-user', draft_id=draft_id,
                                         search_id='fixture-search', candidates=[])
    project_bug_create.confirm_not_duplicate(conn, actor='owner-user', draft_id=draft_id,
                                             expected_digest=expected_digest)
    project_bug_create.mark_ready(conn, actor='owner-user', draft_id=draft_id,
                                  expected_digest=expected_digest)
    project_bug_create.reserve_dispatch(conn, actor='owner-user', draft_id=draft_id,
                                        expected_digest=expected_digest)
    project_bug_create.settle_created(conn, actor='owner-user', draft_id=draft_id,
                                      created_item_id='712345', response_digest='fixture-receipt')
    from k3_support import project_bug_controls
    calls = []

    def bind(*_args, **kwargs):
        calls.append(kwargs['payload'])
        return {'intake_id': 'created-read', 'state': 'queued'}

    monkeypatch.setattr(project_bug_controls, 'execute', bind)
    result = execute_control(conn, cfg, message(
        cfg, f'bug create-bind {draft_id} {expected_digest}', channel, 'created-bind'), control_channel=channel)
    assert 'created-read' in result['text'] and '不会再次创建' in result['text']
    assert calls == [{'draft_id': draft_id, 'expected_digest': expected_digest,
                      'read_hours': 8, 'local_priority': 'P2'}]


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_chat_create_dispatch_unknown_is_consumed_once_and_cannot_bind(conn, creation, monkeypatch, channel):
    cfg, grant, _ = creation
    execute_control(conn, cfg, message(cfg, draft_command(grant, {'title': '软件异常', 'severity': {'value': 'actual-high'}}), channel), control_channel=channel)
    draft_id, digest = conn.execute('SELECT draft_id,request_digest FROM project_bug_create_drafts').fetchone()
    project_bug_create.attach_duplicates(conn, actor='owner-user', draft_id=draft_id,
                                         search_id='fixture-search', candidates=[])
    project_bug_create.confirm_not_duplicate(conn, actor='owner-user', draft_id=draft_id,
                                             expected_digest=digest)
    project_bug_create.mark_ready(conn, actor='owner-user', draft_id=draft_id,
                                  expected_digest=digest)
    from k3_support import project_bug_controls
    original = project_bug_controls.execute
    dispatches = []

    def controlled(conn_, config_, *, action, payload):
        if action == 'dispatch-create-draft':
            dispatches.append(payload)
            project_bug_create.reserve_dispatch(conn_, actor='owner-user', draft_id=draft_id,
                                                expected_digest=digest)
            return project_bug_create.projection(project_bug_create.settle_unknown(
                conn_, actor='owner-user', draft_id=draft_id))
        return original(conn_, config_, action=action, payload=payload)

    monkeypatch.setattr(project_bug_controls, 'execute', controlled)
    command = f'bug create-dispatch {draft_id} {digest}'
    first = execute_control(conn, cfg, message(cfg, command, channel, 'dispatch'), control_channel=channel)
    replay = execute_control(conn, cfg, message(cfg, command, channel, 'dispatch'), control_channel=channel)
    assert '不会重发' in first['text'] and '未重复发送' in replay['text']
    assert len(dispatches) == 1
    with pytest.raises(ValueError, match='creation must be settled'):
        execute_control(conn, cfg, message(
            cfg, f'bug create-bind {draft_id} {digest}', channel, 'bind'), control_channel=channel)
