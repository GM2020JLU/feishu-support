"""Independent verifier jobs use a fresh grant and one pre-bound command."""

import json
import os
import shlex
from uuid import uuid4

import pytest
from test_investigation_candidate import seed
from test_project_verification import definition

from k3_support import project_verification as plans
from k3_support import project_verification_runs as runs
from k3_support import project_verifier_job as verifier
from k3_support.broker_claim_receipts import claim
from k3_support.broker_remote import submit
from k3_support.broker_remote_runner import run_one
from k3_support.broker_start import authorize
from k3_support.coding_catalog import choices, resolve
from k3_support.project_bug_controls import execute
from k3_support.project_investigation_source import selection as source_selection

UID=os.geteuid()+1


def joint_setup(conn, config, tmp_path):
    cfg,request,_,_=setup(conn,config,tmp_path)
    primary=request['repository']
    secondary='companion-firmware'
    cfg.raw['repositories'][secondary]=dict(cfg.raw['repositories'][primary])
    spec=definition()
    first=spec['repositories'][0]
    first.update(repository=primary,branch=request['source']['branch'],node=request['source']['node'],
                 base_commit='a'*40,candidate_commit='b'*40)
    spec['repositories'].append({'id':'companion','node':request['source']['node'],'repository':secondary,
                                 'branch':request['source']['branch'],'base_commit':'c'*40,
                                 'candidate_commit':'d'*40})
    spec['steps'][0].update(layer='software_test',node=request['source']['node'],artifacts=[],devices=[],
                            repositories=['repo','companion'])
    revision=conn.execute('SELECT revision FROM project_bugs').fetchone()[0]
    plan=plans.publish(conn,bug_id=request['bug_id'],round_id=request['round_id'],actor=cfg.control_operator_id,
                       request_id='joint-verification',expected_revision=revision,plan=spec)
    secondary_source={**source_selection(cfg,secondary),'branch':request['source']['branch'],
                      'base_commit':'d'*40,'version':'synthetic'}
    request['verification']={**request['verification'],'plan_id':plan['plan_id'],
                             'sources':{primary:request['source'],secondary:secondary_source}}
    return cfg,request


def test_joint_verifier_admits_exact_candidate_source_set(conn,config,tmp_path):
    cfg,request=joint_setup(conn,config,tmp_path)
    plan,step,primary=verifier.plan_binding(conn,request['verification'],round_id=request['round_id'],
                                            source=request['source'],repository=request['repository'])
    assert [item['id'] for item in (primary,)]==['repo']
    assert step['repositories']==['repo','companion']
    assert set(request['verification']['sources'])=={request['repository'],'companion-firmware'}


def test_joint_verifier_rejects_mismatched_companion_candidate(conn,config,tmp_path):
    cfg,request=joint_setup(conn,config,tmp_path)
    request['verification']['sources']['companion-firmware']['base_commit']='e'*40
    with pytest.raises(ValueError,match='planned candidate'):
        verifier.plan_binding(conn,request['verification'],round_id=request['round_id'],
                              source=request['source'],repository=request['repository'])


@pytest.mark.parametrize('change', ['missing', 'extra', 'wrong_node', 'primary_changed'])
def test_joint_verifier_rejects_incomplete_or_changed_source_authority(conn,config,tmp_path,change):
    cfg,request=joint_setup(conn,config,tmp_path)
    sources=request['verification']['sources']
    if change == 'missing':
        del sources['companion-firmware']
    elif change == 'extra':
        sources['unselected-repository']=dict(sources['companion-firmware'])
    elif change == 'wrong_node':
        sources['companion-firmware']['node']='another-node'
    else:
        sources[request['repository']]={**sources[request['repository']], 'base_commit':'e'*40}
    with pytest.raises(ValueError):
        verifier.plan_binding(conn,request['verification'],round_id=request['round_id'],
                              source=request['source'],repository=request['repository'])


def setup(conn, config, tmp_path, *, base="a"*40, candidate="b"*40, device=False):
    cfg,payload,parent,case,source=seed(conn,config,tmp_path,base=base,candidate=candidate)
    spec=definition();repo=spec['repositories'][0]
    repo.update(repository=payload['repository'],branch=source['branch'],node=source['node'],base_commit=base,candidate_commit=candidate)
    if device:
        spec['steps'][0].update(node=source['node'])
    else:
        spec['steps'][0].update(layer='software_test',node=source['node'],artifacts=[],devices=[])
    revision=conn.execute('SELECT revision FROM project_bugs').fetchone()[0]
    plan=plans.publish(conn,bug_id=payload['bug_id'],round_id=payload['round_id'],actor=cfg.control_operator_id,request_id='verification',expected_revision=revision,plan=spec)
    request=payload | {'source':source,'request_id':'verifier','expected_revision':revision+1,
                       'verification':{'plan_id':plan['plan_id'],'step_id':'function','command':'printf verified-command'}}
    return cfg,request,parent,case


def start(conn, cfg, job):
    descriptor=resolve(cfg,'primary',expected_fingerprint=choices(cfg)['items'][0]['contract_fingerprint'])
    claimed=claim(conn,cfg,{'version':1,'request_id':str(uuid4()),'method':'claim','params':{'pool':'debug'}},
                  peer_uid=UID,control_key=b't'*32,contract_reader=lambda:descriptor)['task']
    assert claimed['job_id']==job['job_id']
    request={'version':1,'request_id':str(uuid4()),'method':'start','params':claimed | {'contract_fingerprint':descriptor.fingerprint}}
    authorize(conn,cfg,request,peer_uid=UID,contract_reader=lambda:descriptor)
    return descriptor,claimed,request


def test_finished_repair_launches_new_plan_bound_verifier_and_executes_exact_step(conn,config,tmp_path):
    cfg,request,parent,_=setup(conn,config,tmp_path)
    job=execute(conn,cfg,action='create-verification-job',payload=request)
    assert execute(conn,cfg,action='create-verification-job',payload=request)['job_id']==job['job_id']
    assert job['job_id']!=parent
    descriptor,task,start_request=start(conn,cfg,job)
    with pytest.raises(ValueError,match='already authorized'):
        authorize(conn,cfg,start_request,peer_uid=UID,contract_reader=lambda:descriptor)
    assert conn.execute('SELECT count(*) FROM project_verification_runs').fetchone()[0]==1
    page=runs.read_for_worker(conn,cfg,{'version':1,'request_id':str(uuid4()),'method':'verification_list','params':task | {'after_id':''}},peer_uid=UID,contract_reader=lambda:descriptor)
    intent=page['items'][0];assert intent['dispatchable']
    remote_request={'version':1,'request_id':intent['remote_request_id'],'method':'remote_submit','params':task | {'remote':intent['remote']}}
    assert submit(conn,cfg,remote_request,peer_uid=UID,contract_reader=lambda:descriptor)['accepted']
    commands=[]
    def transport(**kw):
        kw['heartbeat']()
        if kw.get('keepalive'):
            action=conn.execute("SELECT * FROM broker_remote_actions WHERE state='running'").fetchone()
            binding=json.loads(action['plan_json'])['investigation_checkout'];commands.append(action['request_id'])
            source={'repository':binding['repository'],'path':binding['root']+'/repository','head':'b'*40,'end_head':'b'*40,'base_is_ancestor':True,'tracked_content_matches':True,'tracked_count':1,'matched':True,'coverage':'tracked_source_sample'}
            frame={'job_id':job['job_id'],'request_id':action['request_id'],'base_commit':'b'*40,'created':True,'source':source}
            return {'exit_code':0,'stdout':'K3_CHECKOUT_V1:'+json.dumps(frame)+'\n','stderr':''}
        bindings=json.loads(shlex.split(kw['argv'][-1])[-1])
        sources=[{'repository':b['repository'],'path':b['path'],'head':b['candidate_commit'],'end_head':b['candidate_commit'],'base_is_ancestor':True,'tracked_content_matches':True,'tracked_count':1,'matched':True,'coverage':'tracked_source_sample'} for b in bindings]
        return {'exit_code':0,'stdout':json.dumps({'sources':sources}),'stderr':''}
    for _ in range(2):assert run_one(conn,cfg,contract_reader=lambda:descriptor,transport=transport)['state']=='succeeded'
    assert commands[-1]==intent['remote_request_id'] and len(commands)==2
    view=runs.projection(conn,request['verification']['plan_id'])[0]
    assert view['execution_state']=='succeeded' and view['verification_state']=='unknown'
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0]==0


@pytest.mark.parametrize('mutation',['candidate','branch','repository','round','plan','step','running','actor'])
def test_invalid_verifier_never_enqueues(conn,config,tmp_path,mutation):
    cfg,request,_,_=setup(conn,config,tmp_path)
    if mutation in {'candidate','branch'}:request['source']=request['source'] | ({'base_commit':'c'*40} if mutation=='candidate' else {'branch':'other'})
    elif mutation=='repository':request['repository']='different'
    elif mutation=='round':request['round_id']='different'
    elif mutation in {'plan','step'}:request['verification']=request['verification'] | {mutation+'_id':'different'}
    elif mutation=='running':conn.execute("UPDATE jobs SET state='running'")
    else:request['actor']='model'
    before=conn.execute('SELECT count(*) FROM jobs').fetchone()[0]
    with pytest.raises(ValueError):execute(conn,cfg,action='create-verification-job',payload=request)
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==before


@pytest.mark.parametrize('change',['new_id','new_command'])
def test_verifier_cannot_submit_unprepared_commands(conn,config,tmp_path,change):
    cfg,request,_,_=setup(conn,config,tmp_path);job=verifier.submit(conn,cfg,request)
    descriptor,task,_=start(conn,cfg,job)
    row=conn.execute('SELECT * FROM project_verification_runs').fetchone()
    remote=json.loads(row['remote_json'])
    if change=='new_command':remote['command']='touch unauthorized'
    attempt={'version':1,'request_id':str(uuid4()) if change=='new_id' else row['remote_request_id'],'method':'remote_submit','params':task | {'remote':remote}}
    with pytest.raises(ValueError,match='prepared command'):submit(conn,cfg,attempt,peer_uid=UID,contract_reader=lambda:descriptor)
    assert conn.execute('SELECT count(*) FROM project_verification_workspaces').fetchone()[0]==0


def test_plan_replacement_before_claim_fences_start_without_new_permit(conn,config,tmp_path):
    cfg,request,_,_=setup(conn,config,tmp_path);job=verifier.submit(conn,cfg,request)
    # Simulate a superseding control plan; normal plan publication also requires settlement.
    conn.execute('UPDATE project_bug_rounds SET execution_state=\'planned\'')
    old=conn.execute('SELECT * FROM project_verification_plans').fetchone()
    conn.execute('INSERT INTO project_verification_plans SELECT \'replacement\',round_id,version+1,actor,\'replacement\',request_digest,plan_digest,plan_json,created_at FROM project_verification_plans WHERE plan_id=?',(old['plan_id'],))
    before=conn.execute('SELECT count(*) FROM broker_execution_starts').fetchone()[0]
    with pytest.raises(ValueError,match='no longer current'):start(conn,cfg,job)
    assert conn.execute('SELECT count(*) FROM broker_execution_starts').fetchone()[0]==before


@pytest.mark.parametrize('agent',['claude','dsh','opencode','hermes'])
def test_verifier_start_inbox_is_shared_by_other_coding_contracts(conn,config,tmp_path,agent):
    from test_coding_catalog import configure
    cfg,request,_,_=setup(conn,config,tmp_path)
    configure(cfg,tmp_path,agent)
    request['contract_fingerprint']=choices(cfg)['items'][0]['contract_fingerprint']
    job=verifier.submit(conn,cfg,request);descriptor,task,_=start(conn,cfg,job)
    assert descriptor.agent==agent
    page=runs.read_for_worker(conn,cfg,{'version':1,'request_id':str(uuid4()),'method':'verification_list','params':task | {'after_id':''}},peer_uid=UID,contract_reader=lambda:descriptor)
    assert len(page['items'])==1 and page['items'][0]['dispatchable']


def test_manual_extra_run_cannot_extend_verifier_command_authority(conn,config,tmp_path):
    cfg,request,_,_=setup(conn,config,tmp_path);job=verifier.submit(conn,cfg,request)
    descriptor,task,_=start(conn,cfg,job)
    original=conn.execute('SELECT * FROM project_verification_runs').fetchone()
    duplicate=runs.prepare(conn,plan_id=original['plan_id'],step_id=original['step_id'],grant_id=original['grant_id'],
                           remote_request_id=str(uuid4()),remote=json.loads(original['remote_json']),actor='owner',request_id='additional')
    attempt={'version':1,'request_id':duplicate['remote_request_id'],'method':'remote_submit','params':task | {'remote':json.loads(original['remote_json'])}}
    with pytest.raises(ValueError,match='prepared command'):submit(conn,cfg,attempt,peer_uid=UID,contract_reader=lambda:descriptor)
    cursor=''
    while True:
        page=runs.read_for_worker(conn,cfg,{'version':1,'request_id':str(uuid4()),'method':'verification_list','params':task | {'after_id':cursor}},peer_uid=UID,contract_reader=lambda:descriptor)
        for item in page['items']:
            if item['run_id']==duplicate['run_id']:assert not item['dispatchable']
        cursor=page['next_cursor']
        if cursor is None:break


def test_job_settlement_can_be_checked_without_treating_the_new_verifier_as_its_parent(conn,config,tmp_path):
    from k3_support.project_round_readiness import inspect
    cfg,request,parent,_=setup(conn,config,tmp_path)
    verifier.submit(conn,cfg,request)
    assert inspect(conn,request['round_id'],job_id=parent)['ready']
    assert not inspect(conn,request['round_id'])['ready']
    with pytest.raises(ValueError,match='job unavailable'):inspect(conn,request['round_id'],job_id='unrelated')


def test_device_function_step_creates_verifier_job(conn,config,tmp_path):
    cfg,request,parent,_=setup(conn,config,tmp_path,device=True)
    job=execute(conn,cfg,action='create-verification-job',payload=request)
    assert job['job_id']!=parent
    start(conn,cfg,job)
    assert conn.execute('SELECT count(*) FROM project_verification_runs').fetchone()[0]==1
