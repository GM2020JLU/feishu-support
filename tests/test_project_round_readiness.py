"""Business completion must not hide unsettled broker work; synthetic receipts."""

from uuid import uuid4

import pytest
from test_broker_claim import queued
from test_broker_resources import exited
from test_project_bugs import binding
from test_project_verification import definition

from k3_support import project_bugs as bugs
from k3_support import project_round_readiness as readiness
from k3_support import project_verification as plans
from k3_support.broker_dispatch import finish_observed


def link(conn):
    job=conn.execute('SELECT job_id,case_id FROM jobs').fetchone()
    bug=binding(conn,job['case_id'])
    round_=bugs.start_round(conn,bug_id=bug['bug_id'],actor='owner',request_id='round',
                            reason='Synthetic',expected_revision=bug['revision'])
    conn.execute('INSERT INTO project_investigation_jobs VALUES(?,?)',(job['job_id'],round_['round_id']))
    return bug,round_


def revision(conn,bug):
    return conn.execute('SELECT revision FROM project_bugs WHERE bug_id=?',(bug['bug_id'],)).fetchone()[0]


def publish(conn,bug,round_):
    return plans.publish(conn,bug_id=bug['bug_id'],round_id=round_['round_id'],actor='owner',
                         request_id='plan',expected_revision=revision(conn,bug),plan=definition())


def next_round(conn,bug):
    return bugs.start_round(conn,bug_id=bug['bug_id'],actor='owner',request_id='next',
                             reason='Rework',expected_revision=revision(conn,bug))


@pytest.mark.parametrize('state',['succeeded','failed','cancelled'])
def test_finished_job_waits_for_committed_resource_settlement(conn,config,state):
    exited(conn,config)
    bug,round_=link(conn)
    conn.execute('UPDATE jobs SET state=?',(state,))
    before=list(conn.iterdump())
    view=readiness.inspect(conn,round_['round_id'])
    assert not view['ready'] and not view['resource_operations_performed']
    assert {'launch_unsettled','resources_unsettled'} <= {b['kind'] for b in view['blockers']}
    assert list(conn.iterdump())==before
    with pytest.raises(ValueError,match='resources remain unsettled'):publish(conn,bug,round_)
    with pytest.raises(ValueError,match='resources remain unsettled'):next_round(conn,bug)
    assert finish_observed(conn)=={'finished':1}
    assert readiness.inspect(conn,round_['round_id'])['ready']
    plan=publish(conn,bug,round_)
    assert plan['round_id']==round_['round_id']
    assert bugs.detail(conn,bug['bug_id'])['rounds'][0]['settlement']['ready']
    new=next_round(conn,bug)
    assert new['number']==2
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0]==0


@pytest.mark.parametrize('state',['queued','running','orphaned'])
def test_active_job_blocks_even_if_round_summary_says_finished(conn,state):
    queued(conn)
    bug,round_=link(conn)
    conn.execute('UPDATE jobs SET state=?',(state,))
    conn.execute("UPDATE project_bug_rounds SET execution_state='succeeded'")
    assert readiness.inspect(conn,round_['round_id'])['blockers'][0]['kind']=='job_active'
    with pytest.raises(ValueError,match='resources remain unsettled'):publish(conn,bug,round_)
    with pytest.raises(ValueError,match='resources remain unsettled'):next_round(conn,bug)


def test_uncertain_launch_before_grant_blocks_and_is_not_cancelled_by_view(conn):
    queued(conn)
    bug,round_=link(conn)
    conn.execute("UPDATE jobs SET state='cancelled'")
    job=conn.execute('SELECT job_id,input_digest FROM jobs').fetchone()
    request=str(uuid4())
    conn.execute("INSERT INTO broker_launches VALUES(?,'unknown','now','now')",(request,))
    conn.execute('INSERT INTO broker_launch_bindings VALUES(?,?,?,?,?)',(request,job['job_id'],job['input_digest'],'codex','a'*64))
    before=list(conn.iterdump())
    assert readiness.inspect(conn,round_['round_id'])['blockers']==[{'kind':'launch_unsettled','identity':request,'state':'unknown'}]
    assert list(conn.iterdump())==before
    with pytest.raises(ValueError):next_round(conn,bug)
    assert conn.execute('SELECT state FROM broker_launches').fetchone()[0]=='unknown'


def test_never_dispatched_cancellation_does_not_require_invented_exit(conn):
    queued(conn)
    bug,round_=link(conn)
    conn.execute("UPDATE jobs SET state='cancelled'")
    assert readiness.inspect(conn,round_['round_id'])['ready']
    assert publish(conn,bug,round_)['verification_state']=='not_run'


@pytest.mark.parametrize('state,code',[('queued',None),('unknown',None),('succeeded',255),('failed',124),('succeeded',None)])
def test_remote_work_cannot_hide_behind_settled_job(conn,config,state,code):
    args=exited(conn,config)
    bug,round_=link(conn)
    conn.execute("UPDATE jobs SET state='cancelled'")
    finish_observed(conn)
    request=str(uuid4())
    conn.execute('INSERT INTO broker_remote_actions VALUES(?,1234,?,?,?, ?,?,?)',
                 (request,args['grant_id'],'digest','{}',state,'now','now'))
    if code is not None:
        conn.execute('INSERT INTO broker_remote_results VALUES(?,?,?,?,?)',(request,code,'','','now'))
    assert any(b['kind']=='command_unsettled' for b in readiness.inspect(conn,round_['round_id'])['blockers'])
    with pytest.raises(ValueError):publish(conn,bug,round_)
    with pytest.raises(ValueError):next_round(conn,bug)


def test_settlement_identity_must_match_grant(conn,config):
    exited(conn,config)
    bug,round_=link(conn)
    conn.execute("UPDATE jobs SET state='cancelled'")
    finish_observed(conn)
    conn.execute("UPDATE broker_grants SET input_digest='changed'")
    assert not readiness.inspect(conn,round_['round_id'])['ready']
    with pytest.raises(ValueError):next_round(conn,bug)


def test_unsettled_verification_still_blocks_post_execution_plan(conn,config,monkeypatch):
    from k3_support import project_verification_runs

    queued(conn)
    bug,round_=link(conn)
    conn.execute("UPDATE jobs SET state='cancelled'")
    monkeypatch.setattr(project_verification_runs,'unsettled',lambda *args:True)
    with pytest.raises(ValueError,match='verification execution'):publish(conn,bug,round_)
    with pytest.raises(ValueError,match='verification execution'):next_round(conn,bug)


def test_changed_worker_identity_cannot_reuse_settlement(conn,config):
    exited(conn,config)
    bug,round_=link(conn)
    conn.execute("UPDATE jobs SET state='cancelled'")
    finish_observed(conn)
    conn.execute('UPDATE broker_grants SET worker_uid=worker_uid+1')
    assert not readiness.inspect(conn,round_['round_id'])['ready']
    with pytest.raises(ValueError):publish(conn,bug,round_)


def test_blocker_display_limit_does_not_truncate_readiness_checks(conn,config):
    args=exited(conn,config)
    _,round_=link(conn)
    conn.execute("UPDATE jobs SET state='cancelled'")
    finish_observed(conn)
    for _ in range(25):
        conn.execute("INSERT INTO broker_remote_actions VALUES(?,1234,?,'digest','{}','queued','now','now')",
                     (str(uuid4()),args['grant_id']))
    before=list(conn.iterdump())
    result=readiness.inspect(conn,round_['round_id'])
    assert result['blocker_count']==25 and len(result['blockers'])==20 and not result['ready']
    assert list(conn.iterdump())==before


def test_missing_round_is_not_ready(conn):
    with pytest.raises(ValueError,match='unavailable'):
        readiness.inspect(conn,'missing')
