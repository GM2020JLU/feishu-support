from copy import deepcopy

import pytest
from test_project_investigation import setup

from k3_support import project_investigation as investigation
from k3_support.config import Config
from k3_support.project_investigation_source import require_current, validate


@pytest.mark.parametrize('branch',['main','feature/k3','release/v1.0','fix-123'])
def test_branch_valid(config,conn,tmp_path,branch):
    _,payload=setup(conn,config,tmp_path)
    validate(payload['source'] | {'branch':branch})


@pytest.mark.parametrize('mutation',[
    {'branch':'-x'}, {'branch':'../main'}, {'branch':'a..b'}, {'branch':'main.lock'},
    {'branch':'.hidden'}, {'branch':'x@{y'}, {'branch':'x\\y'}, {'branch':'x y'},
    {'branch':'a//b'}, {'branch':'main.'}, {'branch':'x:y'}, {'branch':'x\n'},
    {'base_commit':'a'*7},{'base_commit':'A'*40},{'base_commit':1},
    {'node':'different'},{'deployment_fingerprint':'0'*64},{'version':'x\n'},
])
def test_bad_or_drifted_source_never_enqueues(conn,config,tmp_path,mutation):
    cfg,payload=setup(conn,config,tmp_path)
    with pytest.raises(ValueError):
        investigation.submit(conn,cfg,payload | {'source':payload['source'] | mutation})
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


@pytest.mark.parametrize('changed',['path','host','work_root'])
def test_deployment_fence_and_replay(conn,config,tmp_path,changed):
    cfg,payload=setup(conn,config,tmp_path)
    job=investigation.submit(conn,cfg,payload)
    raw=deepcopy(cfg.raw)
    if changed=='path': raw['repositories'][payload['repository']]['path'] += '-different'
    elif changed=='host': raw['runtime']['remote_host']='different-node'
    else: raw['runtime']['remote_worktree_root'] += '-different'
    altered=Config(raw,cfg.path)
    with pytest.raises(ValueError,match='deployment changed'):
        require_current(altered,payload['repository'],payload['source'])
    # Reading a receipt does not authorize a second job on the changed deployment.
    assert investigation.submit(conn,altered,payload)['job_id']==job['job_id']


def test_source_intent_is_exposed_without_observation_claim(conn,config,tmp_path):
    cfg,payload=setup(conn,config,tmp_path)
    result=investigation.submit(conn,cfg,payload)
    projected=investigation.projection(conn,payload['bug_id'])[0]
    assert projected['source']==payload['source']
    assert projected['source_observation']=='not_collected'
    from k3_support.executors import ExecutorError, run_codex_job
    with pytest.raises(ExecutorError,match='scoped broker'):
        run_codex_job(conn,cfg,job_id=result['job_id'])


@pytest.fixture
def source_remote(conn,config,monkeypatch):
    import json

    import test_broker_execution_instances as fixtures
    from test_broker_remote_runner import remote
    from test_review import active_config

    from k3_support.ids import canonical_json, digest
    from k3_support.project_investigation_source import selection
    original=fixtures.queued
    def queued(connection):
        original(connection)
        payload=json.loads(connection.execute('SELECT payload_json FROM broker_inputs').fetchone()[0])
        payload['context_extra']['project_investigation']={'source':{
            **selection(active_config(config),'u-boot'),'branch':'main','base_commit':'a'*40,'version':''}}
        connection.execute('UPDATE broker_inputs SET payload_json=?',(canonical_json(payload),))
        connection.execute('UPDATE jobs SET input_digest=?',(digest(payload),))
    monkeypatch.setattr(fixtures,'queued',queued)
    return remote.__wrapped__(conn,config)


def baseline_result(cfg):
    import json
    return {'exit_code':0,'stderr':'','stdout':json.dumps({
        'repository':'u-boot','path':cfg.raw['repositories']['u-boot']['path'],
        'branch':'main','base_commit':'a'*40,'branch_commit':'b'*40,
        'end_branch_commit':'b'*40,'base_is_ancestor':True,
        'coverage':'source_repository_baseline'})}


def checkout_result(conn):
    import json
    binding=json.loads(conn.execute('SELECT plan_json FROM broker_remote_actions ORDER BY rowid DESC LIMIT 1').fetchone()[0])['investigation_checkout']
    source={'repository':binding['repository'],'path':binding['root']+'/repository',
            'head':binding['base_commit'],'end_head':binding['base_commit'],
            'base_is_ancestor':True,'tracked_content_matches':True,'tracked_count':1,
            'matched':True,'coverage':'tracked_source_sample'}
    receipt={'job_id':binding['job_id'],'request_id':binding['request_id'],
             'base_commit':binding['base_commit'],'created':True,'source':source}
    return {'exit_code':0,'stdout':'K3_CHECKOUT_V1:'+json.dumps(receipt)+'\nsynthetic','stderr':''}


@pytest.mark.parametrize('change',['none','path','work_root'])
def test_dispatch_checks_source_deployment_before_transport(conn,source_remote,change):
    from k3_support.broker_remote_runner import run_one
    cfg,reader=source_remote
    raw=deepcopy(cfg.raw)
    if change=='path': raw['repositories']['u-boot']['path']+='-changed'
    if change=='work_root': raw['runtime']['remote_worktree_root']+='-changed'
    calls=[]
    def transport(**kwargs):
        calls.append(True)
        if 'source_repository_baseline' in kwargs['argv'][-1]:
            return baseline_result(cfg)
        kwargs['heartbeat']()
        return checkout_result(conn)
    result=run_one(conn,Config(raw,cfg.path),contract_reader=reader,transport=transport)
    assert result['state']==('succeeded' if change=='none' else 'cancelled')
    assert len(calls)==(3 if change=='none' else 0)


def test_source_change_during_transport_becomes_unknown(conn,source_remote):
    from k3_support.broker_remote_runner import run_one
    cfg,reader=source_remote
    def transport(**kwargs):
        if 'source_repository_baseline' in kwargs['argv'][-1]:
            return baseline_result(cfg)
        cfg.raw['repositories']['u-boot']['path']+='-changed'
        kwargs['heartbeat']()
        pytest.fail('changed source must fence running transport')
    with pytest.raises(ValueError,match='source deployment changed'):
        run_one(conn,cfg,contract_reader=reader,transport=transport)
    assert conn.execute('SELECT state FROM broker_remote_actions').fetchone()[0]=='unknown'
    assert conn.execute('SELECT count(*) FROM broker_remote_results').fetchone()[0]==0


def test_disk_source_change_during_transport_is_detected(conn,source_remote):
    import yaml

    from k3_support.broker_remote_runner import run_one
    from k3_support.project_investigation_source import monitor_source_config
    cfg,reader=source_remote
    cfg.path.write_text(yaml.safe_dump(cfg.raw))
    monitored=monitor_source_config(cfg)
    def transport(**kwargs):
        if 'source_repository_baseline' in kwargs['argv'][-1]:
            return baseline_result(cfg)
        changed=deepcopy(cfg.raw)
        changed['repositories']['u-boot']['path']+='-changed'
        cfg.path.write_text(yaml.safe_dump(changed))
        kwargs['heartbeat']()
        pytest.fail('on-disk source change must fence transport')
    with pytest.raises(ValueError,match='source deployment changed'):
        run_one(conn,monitored,contract_reader=reader,transport=transport)
    assert conn.execute('SELECT state FROM broker_remote_actions').fetchone()[0]=='unknown'
    assert conn.execute('SELECT count(*) FROM broker_remote_results').fetchone()[0]==0
