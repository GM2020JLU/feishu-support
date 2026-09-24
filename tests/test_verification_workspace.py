# ruff: noqa: F811 -- imported fixture
"""Workspace preparation and test are separate journaled broker actions."""

import json
from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID
from test_project_investigation_source import (  # noqa: F401
    baseline_result,
    source_remote,
)
from test_project_verification import definition
from test_verification_sources import observation

from k3_support import project_bugs as bugs
from k3_support import project_verification as plans
from k3_support import project_verification_runs as runs
from k3_support.broker_claim_receipts import claim
from k3_support.broker_remote import submit
from k3_support.broker_remote_runner import run_one


def setup(conn, source_remote, path='workspace', commit='a'*40):
    cfg, reader = source_remote
    conn.execute('DELETE FROM broker_remote_actions')  # discard the never-dispatched fixture request
    job = conn.execute('SELECT * FROM jobs').fetchone()
    bug = bugs.bind(conn,case_id=job['case_id'],host='project.feishu.cn',project_key='fixture',type_key='issue',item_id='123',actor='owner')
    round_ = bugs.start_round(conn,bug_id=bug['bug_id'],actor='owner',request_id='round',reason='Verify',expected_revision=1)
    spec = definition()
    spec['repositories'][0].update(repository='u-boot',branch='main',node=cfg.runtime('remote_host'),base_commit=commit,candidate_commit=commit)
    spec['steps'][0].update(layer='software_test',node=cfg.runtime('remote_host'),artifacts=[],devices=[])
    plan = plans.publish(conn,bug_id=bug['bug_id'],round_id=round_['round_id'],actor='owner',request_id='plan',expected_revision=2,plan=spec)
    claim_id = conn.execute('SELECT request_id FROM broker_claim_receipts').fetchone()[0]
    task = claim(conn,cfg,{'version':1,'request_id':claim_id,'method':'claim','params':{'pool':'debug'}},peer_uid=UID,control_key=b't'*32,now=NOW)['task']
    request_id = str(uuid4())
    remote = {'mode':'work','repo':'u-boot','command':'printf test-executed'}
    root = str(cfg.runtime('remote_worktree_root'))+'/'+job['case_id']+'/investigation-'+job['job_id']+'/repository'
    source = root if path=='workspace' else cfg.raw['repositories']['u-boot']['path']
    run = runs.prepare(conn,plan_id=plan['plan_id'],step_id='function',grant_id=conn.execute('SELECT grant_id FROM broker_grants').fetchone()[0],remote_request_id=request_id,
                       remote=remote,actor='owner',request_id='run',config=cfg,source_paths={'repo':source})
    request = {'version':1,'request_id':request_id,'method':'remote_submit','params':{**task,'remote':remote}}
    return cfg,reader,run,request


def enqueue(conn,ctx):
    cfg,reader,_,request=ctx
    return submit(conn,cfg,request,peer_uid=UID,contract_reader=reader,now=NOW)


def frame(conn,run):
    row=conn.execute("SELECT * FROM broker_remote_actions WHERE state='running'").fetchone()
    plan=json.loads(row['plan_json']);binding=plan['investigation_checkout']
    actual=json.loads(observation(conn,run['run_id'])['stdout'])['sources'][0]
    value={'request_id':row['request_id'],'job_id':binding['job_id'],'base_commit':binding['base_commit'],'created':True,'source':actual}
    return {'exit_code':0,'stdout':'K3_CHECKOUT_V1:'+json.dumps(value)+'\n'+('' if 'verification_workspace_for' in plan else 'test-executed'),'stderr':''}


def test_workspace_completes_before_before_sample_and_test(conn,source_remote):
    ctx=setup(conn,source_remote);cfg,reader,run,request=ctx
    enqueue(conn,ctx);assert enqueue(conn,ctx)['accepted']
    assert conn.execute('SELECT count(*) FROM broker_remote_actions').fetchone()[0]==2
    preparation=conn.execute('SELECT preparation_request_id FROM project_verification_workspaces').fetchone()[0]
    # Sorting must never dispatch the test ahead of its preparation.
    conn.execute("UPDATE broker_remote_actions SET created_at='0000' WHERE request_id=?",(request['request_id'],))
    calls=[]
    def transport(**kw):
        kw['heartbeat']()
        if 'source_repository_baseline' in kw['argv'][-1]:return baseline_result(cfg)
        if kw.get('keepalive'):
            running=conn.execute("SELECT request_id FROM broker_remote_actions WHERE state='running'").fetchone()[0]
            calls.append(running)
            if running==preparation:
                assert conn.execute('SELECT count(*) FROM project_verification_observations').fetchone()[0]==0
            else:
                assert conn.execute("SELECT state FROM project_verification_observations WHERE phase='before'").fetchone()[0]=='matched'
            return frame(conn,run)
        return observation(conn,run['run_id'])
    assert run_one(conn,cfg,contract_reader=reader,transport=transport)['request_id']==preparation
    view=runs.projection(conn,run['plan_id'])[0]
    assert view['execution_state']=='queued' and view['receipt'] is None
    assert view['workspace_preparation']['state']=='succeeded'
    assert run_one(conn,cfg,contract_reader=reader,transport=transport)['state']=='succeeded'
    assert calls==[preparation,request['request_id']]
    assert conn.execute('SELECT count(*) FROM project_investigation_checkouts').fetchone()[0]==2
    assert conn.execute('SELECT stdout FROM broker_remote_results WHERE request_id=?',(request['request_id'],)).fetchone()[0]=='test-executed'
    assert runs.projection(conn,run['plan_id'])[0]['verification_state']=='unknown'


@pytest.mark.parametrize('failure',['known','unknown','missing_frame','revoked','receipt_failure'])
def test_failed_workspace_never_runs_test_or_retries(conn,source_remote,failure):
    import sqlite3
    ctx=setup(conn,source_remote);cfg,reader,run,request=ctx;enqueue(conn,ctx)
    if failure=='receipt_failure':
        conn.execute("CREATE TEMP TRIGGER fail_receipt BEFORE INSERT ON broker_remote_results BEGIN SELECT RAISE(ABORT,'fixture'); END")
    def transport(**kw):
        if 'source_repository_baseline' in kw['argv'][-1]:return baseline_result(cfg)
        if not kw.get('keepalive'):return observation(conn,run['run_id'])
        if failure=='revoked':
            conn.execute("UPDATE broker_grants SET revoked_at='now'");kw['heartbeat']()
        if failure=='unknown':return frame(conn,run) | {'exit_code':255}
        return {'exit_code':126 if failure=='known' else 0,'stdout':'','stderr':''} if failure!='receipt_failure' else frame(conn,run)
    if failure in {'missing_frame','revoked','receipt_failure'}:
        with pytest.raises((ValueError,sqlite3.IntegrityError)):run_one(conn,cfg,contract_reader=reader,transport=transport)
    else:
        assert run_one(conn,cfg,contract_reader=reader,transport=transport)['state']==('failed' if failure=='known' else 'unknown')
    result=run_one(conn,cfg,contract_reader=reader,transport=lambda **kw:pytest.fail('test must not execute'))
    assert result['state']==('cancelled' if failure=='known' else 'occupied')
    assert conn.execute('SELECT count(*) FROM broker_remote_results WHERE request_id=?',(request['request_id'],)).fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM project_verification_observations').fetchone()[0]==0


def test_wrong_evidence_path_rolls_back_both_actions(conn,source_remote):
    ctx=setup(conn,source_remote,path='source')
    with pytest.raises(ValueError,match='exact isolated'):enqueue(conn,ctx)
    assert conn.execute('SELECT count(*) FROM broker_remote_actions').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM project_verification_workspaces').fetchone()[0]==0


def test_paused_plan_cancels_preparation_and_test_without_transport(conn,source_remote):
    ctx=setup(conn,source_remote);cfg,reader,_,_=ctx;enqueue(conn,ctx)
    conn.execute("UPDATE project_bug_rounds SET execution_state='paused'")
    for _ in range(2):
        assert run_one(conn,cfg,contract_reader=reader,transport=lambda **kw:pytest.fail('paused'))['state']=='cancelled'
    assert conn.execute('SELECT count(*) FROM broker_remote_results').fetchone()[0]==0


def test_workspace_link_failure_rolls_back_queue(conn,source_remote):
    import sqlite3
    ctx=setup(conn,source_remote)
    conn.execute("CREATE TEMP TRIGGER fail_link BEFORE INSERT ON project_verification_workspaces BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError):enqueue(conn,ctx)
    assert conn.execute('SELECT count(*) FROM broker_remote_actions').fetchone()[0]==0


def test_preparation_and_test_have_distinct_durable_journal_identities(conn,source_remote):
    import sqlite3
    ctx=setup(conn,source_remote);cfg,_,_,request=ctx
    cfg.raw['runtime']['remote_receipt_directory']='/srv/private-receipts'
    enqueue(conn,ctx)
    records=conn.execute('SELECT * FROM broker_remote_actions').fetchall()
    assert len(records)==2
    assert len({json.loads(row['plan_json'])['command_digest'] for row in records})==2
    for row in records:
        plan=json.loads(row['plan_json'])
        assert plan['guard_version']==2 and row['request_id'] in plan['command']
    link=conn.execute('SELECT * FROM project_verification_workspaces').fetchone()
    assert link['request_id']==request['request_id']
    for sql in ["UPDATE project_verification_workspaces SET request_id=request_id",'DELETE FROM project_verification_workspaces']:
        with pytest.raises(sqlite3.IntegrityError):conn.execute(sql)


@pytest.mark.parametrize('code',[None,0,255])
def test_preparation_summary_requires_exit_and_checkout_receipts(conn,source_remote,code):
    from k3_support.project_verification_workspace import projection
    ctx=setup(conn,source_remote);cfg,reader,_,request=ctx;enqueue(conn,ctx)
    preparation=conn.execute('SELECT preparation_request_id FROM project_verification_workspaces').fetchone()[0]
    conn.execute("UPDATE broker_remote_actions SET state='succeeded' WHERE request_id=?",(preparation,))
    if code is not None:
        conn.execute('INSERT INTO broker_remote_results VALUES(?,?,?,?,?)',(preparation,code,'','','now'))
    assert projection(conn,request['request_id'])['state']=='unknown'
    assert run_one(conn,cfg,contract_reader=reader,transport=lambda **kw:pytest.fail('unproven'))['state'] in {'cancelled','occupied'}
