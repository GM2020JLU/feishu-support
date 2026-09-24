"""Created receipts enter the existing read queue; no remote creation is retried."""
# ruff: noqa: F811 -- imported pytest fixture
from datetime import timedelta

import pytest
from test_project_link_intake import context, run  # noqa: F401

from k3_support import project_bug_create as drafts
from k3_support import project_create_grants as grants
from k3_support.project_bug_controls import execute
from k3_support.timeutil import utc_now


def created(conn, *, project='space', state='created'):
    scope={'host':'project.feishu.cn','project_key':project,'type_key':'issue'}
    grant=grants.issue(conn,actor='owner',request_id='g',scope=scope|{'max_creations':1},expires_at=(utc_now()+timedelta(hours=1)).isoformat())
    row=drafts.prepare(conn,actor='owner',request_id='draft',grant_id=grant['grant_id'],field_values={'name':'fixture'},required_fields=[],**scope)
    if state=='created':
        drafts.attach_duplicates(conn,actor='owner',draft_id=row['draft_id'],search_id='fixture-search',candidates=[])
        drafts.confirm_not_duplicate(conn,actor='owner',draft_id=row['draft_id'],expected_digest=row['request_digest'])
        drafts.mark_ready(conn,actor='owner',draft_id=row['draft_id'],expected_digest=row['request_digest'])
        drafts.reserve_dispatch(conn,actor='owner',draft_id=row['draft_id'],expected_digest=row['request_digest'])
        drafts.settle_created(conn,actor='owner',draft_id=row['draft_id'],created_item_id='123',response_digest='fixture-only')
    return {'draft_id':row['draft_id'],'expected_digest':row['request_digest'],'read_hours':8,'local_priority':'P2'}


@pytest.mark.parametrize('project',['space','k3'])
def test_created_draft_binding_replays_and_observes_before_local_creation(conn,config,context,project):
    request=created(conn,project=project)
    queued=execute(conn,config,action='bind-created-draft',payload=request)
    assert queued['state']=='queued'
    assert execute(conn,config,action='bind-created-draft',payload=request)==queued
    assert conn.execute('SELECT count(*) FROM project_bugs').fetchone()[0]==0
    result=run(conn,config)
    assert result['state']=='succeeded'
    assert result['snapshot_id'] and result['bug_id']
    assert execute(conn,config,action='bind-created-draft',payload=request)==result
    assert conn.execute('SELECT count(*) FROM project_link_intakes').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM project_bugs').fetchone()[0]==1
    assert conn.execute('SELECT state FROM project_bug_create_drafts').fetchone()[0]=='created'


@pytest.mark.parametrize('bad',['draft','digest','owner','scope'])
def test_invalid_created_binding_does_not_enqueue(conn,config,context,bad):
    request=created(conn,state='draft' if bad=='draft' else 'created')
    if bad=='digest':request['expected_digest']='wrong'
    if bad=='owner':config.raw['identity']['control_operator_id']='other'
    if bad=='scope':config.raw['project_integration']['intake_spaces']=[]
    with pytest.raises((ValueError,PermissionError)):
        execute(conn,config,action='bind-created-draft',payload=request)
    assert conn.execute('SELECT count(*) FROM project_link_intakes').fetchone()[0]==0


@pytest.mark.parametrize('state', ['failed', 'blocked'])
def test_failed_created_read_has_one_retry_and_preserves_creation(conn, config, context, state):
    request = created(conn)
    first = execute(conn, config, action='bind-created-draft', payload=request)
    # A terminal read failure is a fault fixture, not a native creation failure.
    conn.execute('UPDATE project_link_intakes SET state=? WHERE intake_id=?',
                 (state, first['intake_id']))
    retry = request | {'retry_intake_id': first['intake_id']}
    second = execute(conn, config, action='retry-created-draft-read', payload=retry)
    assert second['intake_id'] != first['intake_id']
    assert execute(conn, config, action='retry-created-draft-read', payload=retry) == second
    assert run(conn, config)['state'] == 'succeeded'
    assert execute(conn, config, action='retry-created-draft-read', payload=retry)['state'] == 'succeeded'
    assert conn.execute('SELECT state FROM project_link_intakes WHERE intake_id=?',
                        (first['intake_id'],)).fetchone()[0] == state
    assert conn.execute('SELECT count(*) FROM project_bugs').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM project_bug_create_drafts WHERE state="created"').fetchone()[0] == 1


@pytest.mark.parametrize('state', ['queued', 'running', 'succeeded'])
def test_created_read_retry_refuses_nonfailure(conn, config, context, state):
    request = created(conn)
    first = execute(conn, config, action='bind-created-draft', payload=request)
    if state == 'succeeded':
        assert run(conn, config)['state'] == 'succeeded'
    elif state == 'running':
        conn.execute('UPDATE project_link_intakes SET state=?,lease_token=?,lease_expires_at=? WHERE intake_id=?',
                     (state, 'fixture-lease', (utc_now()+timedelta(minutes=1)).isoformat(), first['intake_id']))
    with pytest.raises(ValueError, match='terminal failed'):
        execute(conn, config, action='retry-created-draft-read',
                payload=request | {'retry_intake_id': first['intake_id']})
    assert conn.execute('SELECT count(*) FROM project_link_intakes').fetchone()[0] == 1


def test_created_read_retry_refuses_unrelated_attempt_and_rechecks_scope(conn, config, context):
    from k3_support import project_link_intake

    request = created(conn)
    unrelated = project_link_intake.enqueue(conn, config, **context)
    conn.execute('UPDATE project_link_intakes SET state="failed" WHERE intake_id=?',
                 (unrelated['intake_id'],))
    with pytest.raises(PermissionError, match='belong'):
        execute(conn, config, action='retry-created-draft-read',
                payload=request | {'retry_intake_id': unrelated['intake_id']})
    first = execute(conn, config, action='bind-created-draft', payload=request)
    conn.execute('UPDATE project_link_intakes SET state="failed" WHERE intake_id=?',
                 (first['intake_id'],))
    config.raw['project_integration']['intake_spaces'] = []
    with pytest.raises(PermissionError, match='scope'):
        execute(conn, config, action='retry-created-draft-read',
                payload=request | {'retry_intake_id': first['intake_id']})
    assert conn.execute('SELECT count(*) FROM project_link_intakes').fetchone()[0] == 2
