# ruff: noqa: F811 -- pytest provides imported broker fixtures
"""Synthetic broker/collector receipts; no claim of real functional acceptance."""

import copy
import sqlite3
from uuid import uuid4

import pytest
from test_project_verification_runs import context, enqueue  # noqa: F401
from test_verification_sources import observation

from k3_support import project_verification as plans
from k3_support import project_verification_reviews as reviews
from k3_support import project_verification_runs as runs
from k3_support.broker_remote_runner import run_one


def execute_fixture(conn, context, *, layer='software_test', artifacts=False, devices=False, depends=False, code=0, after=True, output=None):
    cfg, descriptor, bug, round_, prior, intent, request = context
    definition=copy.deepcopy(prior['definition'])
    definition['repositories'][0]['node']=cfg.runtime('remote_host')
    step=definition['steps'][0]
    step.update(node=cfg.runtime('remote_host'),layer=layer,devices=['board'] if devices else [],artifacts=['image'] if artifacts else [])
    if depends:
        definition['steps'].append(step|{'id':'dependent','title':'Dependent','depends_on':['function']})
    plan=plans.publish(conn,bug_id=bug['bug_id'],round_id=round_['round_id'],actor='owner',
                       request_id='review-plan',expected_revision=3,plan=definition)
    intent=intent|{'plan_id':plan['plan_id'],'config':cfg,
                   'source_paths':{'repo':cfg.raw['repositories']['u-boot']['path']}}
    prepared=runs.prepare(conn,**intent)
    ctx=cfg,descriptor,bug,round_,plan,intent,request
    enqueue(conn,ctx)
    sampled=[]
    def transport(**kw):
        kw['heartbeat']()
        if kw.get('keepalive'):
            return {'exit_code':code,'stdout':output if output is not None else 'fixture output '*1300,'stderr':'fixture error'}
        sampled.append(True)
        return observation(conn,prepared['run_id'],match=after or len(sampled)==1)
    run_one(conn,cfg,contract_reader=lambda:descriptor,transport=transport)
    return ctx,prepared


def payload(conn, run, verdict='passed'):
    view=reviews.detail(conn,run['run_id'])
    return {'run_id':run['run_id'],'request_id':str(uuid4()),'evidence_digest':view['evidence_digest'],
            'expected_review_id':view['review']['review_id'] if view['review'] else None,
            'verdict':verdict,'rationale':'Fixture operator checked procedure, environment and oracle.', 'attested':True}


def test_zero_exit_needs_explicit_review_and_does_not_close_or_mark_repaired(conn,context):
    from k3_support import project_bugs
    from k3_support.project_investigation_result import draft

    ctx,run=execute_fixture(conn,context)
    assert plans.current(conn,ctx[3]['round_id'])['verification_state']=='unknown'
    request=payload(conn,run)
    result=reviews.record(conn,actor='owner',payload=request)
    before=list(conn.iterdump())
    assert reviews.record(conn,actor='owner',payload=request)==result
    assert list(conn.iterdump())==before
    current=plans.current(conn,ctx[3]['round_id'])
    assert current['verification_state']=='passed'
    assert current['operator_verification']['verdict_source']=='operator_review'
    assert current['operator_verification']['closure_authorized'] is False
    job_id=conn.execute('SELECT job_id FROM jobs').fetchone()[0]
    conn.execute('INSERT INTO project_investigation_jobs VALUES(?,?)',(job_id,ctx[3]['round_id']))
    assert project_bugs.detail(conn,ctx[2]['bug_id'])['rounds'][0]['verification_state']=='passed'
    result=draft(conn,bug_id=ctx[2]['bug_id'],job_id=job_id)
    assert result['verification_state']=='passed' and result['functional_verdict']=='operator_reviewed_pass'
    assert conn.execute('SELECT repair_state FROM project_bug_rounds').fetchone()[0]=='not_started'
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0]==0


@pytest.mark.parametrize('kwargs,blocker',[({'code':1},'execution_not_successful'),({'after':False},'source_not_verified'),({'artifacts':True},'artifact_evidence_unavailable'),({'devices':True},'device_evidence_unavailable')])
def test_pass_requires_bound_evidence(conn,context,kwargs,blocker):
    _,run=execute_fixture(conn,context,**kwargs)
    assert blocker in reviews.detail(conn,run['run_id'])['pass_blockers']
    with pytest.raises(ValueError,match='complete bound evidence'):
        reviews.record(conn,actor='owner',payload=payload(conn,run))
    assert conn.execute('SELECT count(*) FROM project_verification_reviews').fetchone()[0]==0


def test_device_step_blocked_without_echoed_evidence(conn,context):
    _,run=execute_fixture(conn,context,layer='device_function',devices=True,artifacts=True)
    blockers=reviews.detail(conn,run['run_id'])['pass_blockers']
    assert 'device_evidence_unavailable' in blockers and 'artifact_evidence_unavailable' in blockers
    with pytest.raises(ValueError,match='complete bound evidence'):
        reviews.record(conn,actor='owner',payload=payload(conn,run))


@pytest.mark.parametrize('prefix', [
    'boot: device fixture-only online',
    'echo fixture-only',
    'ERROR: fixture-only disconnected; expected image',
])
def test_printed_identity_and_digest_cannot_prove_device_verification(conn,context,prefix):
    evidence=prefix+'\nartifact sha256 '+'c'*64+'\nSELFTEST PASS'
    ctx,run=execute_fixture(conn,context,layer='device_function',devices=True,artifacts=True,output=evidence)
    view=reviews.detail(conn,run['run_id'])
    assert view['review_available']
    assert 'device_evidence_unavailable' in view['pass_blockers']
    assert 'artifact_evidence_unavailable' in view['pass_blockers']
    with pytest.raises(ValueError,match='complete bound evidence'):
        reviews.record(conn,actor='owner',payload=payload(conn,run))
    assert conn.execute('SELECT count(*) FROM project_verification_reviews').fetchone()[0]==0
    reviews.record(conn,actor='owner',payload=payload(conn,run,'inconclusive'))
    assert reviews.detail(conn,run['run_id'])['state']=='inconclusive'
    assert plans.current(conn,ctx[3]['round_id'])['verification_state']!='passed'


def test_legacy_echo_only_pass_cannot_authorize_closure(conn,context,monkeypatch):
    from datetime import timedelta

    from k3_support import project_close_gate as close_gate
    from k3_support.timeutil import utc_now

    evidence='fixture-only '+ 'c'*64+' SELFTEST PASS'
    ctx,run=execute_fixture(conn,context,layer='device_function',devices=True,artifacts=True,output=evidence)
    # Emulate the old admission rule only to create its immutable historical
    # review. The real current assessment and closure queries run unmocked.
    with monkeypatch.context() as old:
        old.setattr(reviews,'_device_evidence_blockers',lambda *args: [])
        receipt=reviews.record(conn,actor='owner',payload=payload(conn,run))
        assert reviews.detail(conn,run['run_id'])['state']=='passed'
    assert receipt['verdict']=='passed'
    assert reviews.detail(conn,run['run_id'])['state']=='stale'
    assert close_gate.evidence(conn,ctx[2]['bug_id'])['verification_state']!='passed'
    with pytest.raises(ValueError,match='passed verification'):
        close_gate.request(conn,actor='owner',request_id='legacy-echo-close',bug_id=ctx[2]['bug_id'],
                           change={'transition_id':'close','target_status_id':'closed'},
                           expires_at=(utc_now()+timedelta(hours=1)).isoformat())
    assert conn.execute('SELECT count(*) FROM project_close_approvals').fetchone()[0]==0


@pytest.mark.parametrize('layer',['static','build'])
def test_build_or_static_only_plan_never_passes_whole_bug(conn,context,layer):
    ctx,run=execute_fixture(conn,context,layer=layer)
    reviews.record(conn,actor='owner',payload=payload(conn,run))
    assert reviews.detail(conn,run['run_id'])['state']=='passed'
    current=plans.current(conn,ctx[3]['round_id'])
    assert current['verification_state']=='unknown'
    assert current['operator_verification']['functional_step_required'] is False


@pytest.mark.parametrize('change', ["UPDATE jobs SET attempt_no=attempt_no+1", "UPDATE jobs SET input_digest='changed'", "UPDATE cases SET lifecycle_round=lifecycle_round+1"])
def test_binding_change_invalidates_review(conn,context,change):
    ctx,run=execute_fixture(conn,context)
    request=payload(conn,run)
    reviews.record(conn,actor='owner',payload=request)
    conn.execute(change)
    view=reviews.detail(conn,run['run_id'])
    assert view['state']=='stale' and not view['review_available']
    assert plans.current(conn,ctx[3]['round_id'])['verification_state']!='passed'
    assert reviews.record(conn,actor='owner',payload=request)['verdict']=='passed'  # immutable historical receipt


def test_review_correction_is_append_only_and_concurrent_review_conflicts(conn,context):
    _,run=execute_fixture(conn,context)
    request=payload(conn,run)
    first=reviews.record(conn,actor='owner',payload=request)
    with pytest.raises(ValueError,match='changed'):
        reviews.record(conn,actor='owner',payload=request|{'request_id':'concurrent'})
    second=reviews.record(conn,actor='owner',payload=payload(conn,run,'failed'))
    assert second['review_id']!=first['review_id']
    assert reviews.detail(conn,run['run_id'])['state']=='failed'
    with pytest.raises(ValueError,match='reused'):
        reviews.record(conn,actor='owner',payload=request|{'rationale':'different'})
    for sql in ["UPDATE project_verification_reviews SET verdict='inconclusive'", 'DELETE FROM project_verification_reviews']:
        with pytest.raises(sqlite3.IntegrityError):conn.execute(sql)


def test_paged_evidence_is_complete_and_fenced(conn,context):
    _,run=execute_fixture(conn,context)
    view=reviews.detail(conn,run['run_id'])
    offset=0; chunks=[]
    while offset is not None:
        page=reviews.output(conn,run_id=run['run_id'],evidence_digest=view['evidence_digest'],channel='stdout',offset=offset)
        assert len(page['text'])<=reviews.PAGE_SIZE
        chunks.append(page['text']);offset=page['next_offset']
    assert ''.join(chunks)=='fixture output '*1300
    for change in [{'evidence_digest':'wrong'}, {'offset':True}, {'offset':-1}, {'channel':'credential'}]:
        with pytest.raises(ValueError):reviews.output(conn,**({'run_id':run['run_id'],'evidence_digest':view['evidence_digest'],'channel':'stdout','offset':0}|change))


@pytest.mark.parametrize('field,value',[('attested',False),('attested',1),('rationale',''),('verdict','closed'),('verdict',[])])
def test_explicit_attestation_schema(conn,context,field,value):
    _,run=execute_fixture(conn,context)
    with pytest.raises(ValueError):reviews.record(conn,actor='owner',payload=payload(conn,run)|{field:value})


def test_unknown_execution_is_only_inconclusive(conn,context):
    _,run=execute_fixture(conn,context,code=255)
    for verdict in ['passed','failed']:
        with pytest.raises(ValueError):reviews.record(conn,actor='owner',payload=payload(conn,run,verdict))
    reviews.record(conn,actor='owner',payload=payload(conn,run,'inconclusive'))
    assert reviews.detail(conn,run['run_id'])['state']=='inconclusive'


def test_new_plan_invalidates_old_review(conn,context):
    ctx,run=execute_fixture(conn,context)
    reviews.record(conn,actor='owner',payload=payload(conn,run))
    revision=conn.execute('SELECT revision FROM project_bugs').fetchone()[0]
    plan=copy.deepcopy(ctx[4]['definition']);plan['repositories'][0]['candidate_commit']='d'*40
    next_plan=plans.publish(conn,bug_id=ctx[2]['bug_id'],round_id=ctx[3]['round_id'],actor='owner',request_id='replacement',expected_revision=revision,plan=plan)
    assert next_plan['verification_state']=='not_run'
    assert reviews.detail(conn,run['run_id'])['state']=='stale'


def test_new_run_does_not_reuse_previous_success(conn,context):
    ctx,run=execute_fixture(conn,context)
    reviews.record(conn,actor='owner',payload=payload(conn,run))
    new=runs.prepare(conn,**(ctx[-2]|{'request_id':'again','remote_request_id':str(uuid4())}))
    with pytest.raises(ValueError,match='newer run'):reviews.detail(conn,run['run_id'])
    assert reviews.detail(conn,new['run_id'])['review'] is None
    assert reviews.detail(conn,new['run_id'])['state']=='not_run'
    assert plans.current(conn,ctx[3]['round_id'])['verification_state']!='passed'


def test_dependency_execution_requires_current_review_and_freezes_it(conn,context):
    ctx,run=execute_fixture(conn,context,depends=True)
    intent=ctx[-2]|{'step_id':'dependent','request_id':'dependent','remote_request_id':str(uuid4())}
    with pytest.raises(ValueError,match='dependencies'):runs.prepare(conn,**intent)
    reviews.record(conn,actor='owner',payload=payload(conn,run))
    child=runs.prepare(conn,**intent)
    reviews.require_dependencies(conn,child)
    reviews.record(conn,actor='owner',payload=payload(conn,run))  # a fresh review changes dependency evidence
    with pytest.raises(ValueError,match='dependency evidence changed'):
        reviews.require_dependencies(conn,child)
    request=ctx[-1]|{'request_id':intent['remote_request_id']}
    with pytest.raises(ValueError,match='dependency evidence changed'):
        enqueue(conn,ctx,request=request)


def test_audit_failure_rolls_back_decision_and_revision(conn,context,monkeypatch):
    _,run=execute_fixture(conn,context)
    request=payload(conn,run)
    before=list(conn.iterdump())
    def fail(*args):raise RuntimeError('injected')
    monkeypatch.setattr(reviews,'_event',fail)
    with pytest.raises(RuntimeError):reviews.record(conn,actor='owner',payload=request)
    assert list(conn.iterdump())==before


def test_reviewed_dependencies_execute_and_correction_invalidates_child_pass(conn,context):
    ctx,run=execute_fixture(conn,context,depends=True)
    reviews.record(conn,actor='owner',payload=payload(conn,run))
    intent=ctx[-2]|{'step_id':'dependent','request_id':'dependent','remote_request_id':str(uuid4())}
    child=runs.prepare(conn,**intent)
    enqueue(conn,ctx,request=ctx[-1]|{'request_id':intent['remote_request_id']})
    def transport(**kw):
        kw['heartbeat']()
        return {'exit_code':0,'stdout':'dependent fixture','stderr':''} if kw.get('keepalive') else observation(conn,child['run_id'])
    run_one(conn,ctx[0],contract_reader=lambda:ctx[1],transport=transport)
    reviews.record(conn,actor='owner',payload=payload(conn,child))
    assert plans.current(conn,ctx[3]['round_id'])['verification_state']=='passed'
    reviews.record(conn,actor='owner',payload=payload(conn,run,'failed'))
    assert reviews.detail(conn,child['run_id'])['state']=='stale'
    assert plans.current(conn,ctx[3]['round_id'])['verification_state']=='failed'


def test_control_route_records_server_operator_and_rejects_injected_actor(conn,context):
    from k3_support.project_bug_controls import execute

    ctx,run=execute_fixture(conn,context)
    request=payload(conn,run)
    with pytest.raises(ValueError,match='exact request'):
        execute(conn,ctx[0],action='record-verification-review',payload=request|{'actor':'model'})
    result=execute(conn,ctx[0],action='record-verification-review',payload=request)
    assert result['actor']==ctx[0].control_operator_id
