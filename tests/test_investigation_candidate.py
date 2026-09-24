"""Synthetic control receipts; native Git isolation is accepted separately."""

from uuid import uuid4

import pytest
from test_project_investigation import setup

from k3_support import project_bugs as bugs
from k3_support import project_investigation as investigation
from k3_support import project_investigation_candidate as candidates
from k3_support import project_investigation_checkout as checkout
from k3_support.ids import canonical_json


def seed(conn, config, tmp_path, *, base="a"*40, candidate="b"*40, changeset=None):
    cfg, payload = setup(conn, config, tmp_path)
    payload['source']['base_commit'] = base
    job = investigation.submit(conn, cfg, payload)['job_id']
    conn.execute("UPDATE jobs SET state='succeeded',attempt_no=1 WHERE job_id=?", (job,))
    row = conn.execute('SELECT * FROM jobs WHERE job_id=?', (job,)).fetchone()
    case = row['case_id']
    conn.execute('INSERT INTO broker_grants VALUES(?,?,?,?,?,?,?,?,?,?,NULL)',
                 ('seed-grant','a'*64,1234,job,1,row['lifecycle_round'],row['input_digest'],'fixture','now','later'))
    conn.execute('INSERT INTO broker_execution_starts VALUES(?,?,?,?,?,?)',
                 ('seed-grant',job,1,'start',1234,'now'))
    conn.execute('INSERT INTO broker_execution_resources VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                 ('seed-grant','claim',job,1,row['lifecycle_round'],row['input_digest'],case,None,1,'now','settled'))
    request = str(uuid4())
    root = str(cfg.runtime('remote_worktree_root')) + '/' + case + '/investigation-' + job
    binding = {'job_id':job,'request_id':request,'root':root,'repository':payload['repository'],
               'base_commit':payload['source']['base_commit']}
    conn.execute("INSERT INTO broker_remote_actions VALUES(?,1234,'seed-grant','fixture',?,'succeeded','now','now')",
                 (request,canonical_json({'investigation_checkout':binding})))
    conn.execute('INSERT INTO broker_remote_results VALUES(?,0,?,?,?)',(request,'','','now'))
    observed = {'repository':payload['repository'],'path':root+'/repository','head':candidate,'end_head':candidate,
                'base_is_ancestor':True,'tracked_content_matches':True,'matched':False,
                'tracked_count':1,'coverage':'tracked_source_sample'}
    after_observation = {'state':'clean','source':observed}
    if changeset is not None:
        after_observation['changeset'] = changeset
    conn.execute('INSERT INTO project_investigation_checkout_after VALUES(?,?,?,?,?)',
                 (request,job,'clean',canonical_json(after_observation),'now'))
    source = payload['source'] | {'candidate_request_id':request,'base_commit':candidate}
    return cfg, payload, job, case, source


def test_candidate_is_same_case_settled_commit_without_functional_verdict(conn,config,tmp_path):
    cfg, payload, job, case, source = seed(conn,config,tmp_path)
    before=list(conn.iterdump())
    found=candidates.choices(conn,cfg,case)
    assert len(found)==1 and found[0]['source']==source and found[0]['job_id']==job
    assert 'path' not in found[0]
    assert list(conn.iterdump())==before
    round_=bugs.start_round(conn,bug_id=payload['bug_id'],actor='owner',request_id='next',reason='Verify candidate',
                            expected_revision=conn.execute('SELECT revision FROM project_bugs').fetchone()[0])
    request=payload | {'round_id':round_['round_id'],'expected_revision':conn.execute('SELECT revision FROM project_bugs').fetchone()[0],
                       'source':source,'request_id':'next-job'}
    created=investigation.submit(conn,cfg,request)
    assert investigation.submit(conn,cfg,request)['job_id']==created['job_id']
    inputs={'repos':[payload['repository']],'investigation_source':source}
    command,binding=checkout.prepare(conn,cfg,inputs,job_id=created['job_id'],case_id=case,request_id='next-command',
                                     remote={'mode':'work','repo':payload['repository'],'command':'true'})
    assert binding['seed_job_id']==job and binding['source_path'].endswith('/investigation-'+job+'/repository')
    assert binding['root'] not in binding['source_path'] and 'candidate_source_changed' in command
    checkout.validate_plan(cfg,inputs,{'investigation_checkout':binding},job_id=created['job_id'],case_id=case,request_id='next-command',conn=conn)
    with pytest.raises(ValueError,match='binding changed'):
        checkout.validate_plan(cfg,inputs,{'investigation_checkout':binding | {'source_path':'/elsewhere'}},job_id=created['job_id'],case_id=case,request_id='next-command',conn=conn)
    assert conn.execute('SELECT verification_state FROM project_bug_rounds WHERE round_id=?',(round_['round_id'],)).fetchone()[0]=='not_run'


@pytest.mark.parametrize('mutation',['other_case','other_repository','other_commit','other_branch','running','later_command','unknown_exit','no_receipt','no_settlement','no_start','changed_attempt','input_changed','deployment_changed'])
def test_candidate_rejects_stale_or_unsettled_provenance(conn,config,tmp_path,mutation):
    cfg,payload,_job,case,source=seed(conn,config,tmp_path)
    repository=payload['repository']
    if mutation=='other_case':case='different'
    elif mutation=='other_repository':repository='different'
    elif mutation=='other_commit':source=source | {'base_commit':'c'*40}
    elif mutation=='other_branch':source=source | {'branch':'different'}
    elif mutation=='running':conn.execute("UPDATE jobs SET state='running'")
    elif mutation=='later_command':conn.execute("INSERT INTO broker_remote_actions VALUES('later',1234,'seed-grant','fixture','{}','cancelled','now','now')")
    elif mutation=='unknown_exit':conn.execute('UPDATE broker_remote_results SET exit_code=255')
    elif mutation=='no_receipt':conn.execute('DELETE FROM broker_remote_results')
    elif mutation=='no_settlement':conn.execute('DELETE FROM broker_execution_resources')
    elif mutation=='no_start':
        conn.execute('DELETE FROM broker_execution_resources');conn.execute('DELETE FROM broker_execution_starts')
    elif mutation=='changed_attempt':conn.execute('UPDATE jobs SET attempt_no=2')
    elif mutation=='input_changed':conn.execute("UPDATE jobs SET input_digest='changed'")
    elif mutation=='deployment_changed':cfg.raw['runtime']['remote_worktree_root']+='-other'
    with pytest.raises(ValueError):candidates.resolve(conn,cfg,case_id=case,repository=repository,source=source)
    if mutation not in {'other_case','other_repository','other_commit','other_branch'}:
        assert candidates.choices(conn,cfg,case)==[]


def test_bad_candidate_never_creates_job(conn,config,tmp_path):
    cfg,payload=setup(conn,config,tmp_path)
    with pytest.raises(ValueError,match='control evidence'):
        investigation.submit(conn,cfg,payload | {'source':payload['source'] | {'candidate_request_id':'missing'}})
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


def test_seed_mount_is_readonly_and_only_repository(config):
    from k3_support.remote_sandbox import sandbox_argv
    args={'case_id':'case','source_root':'/srv/source','worktree_root':'/srv/work',
          'repo_paths':['/srv/source/repo'],'toolchain_roots':[], 'writable':True,'command':'true','work_id':'new','seed_work_id':'old'}
    argv=sandbox_argv(**args)
    offset=argv.index('/srv/work/case/investigation-old/repository')
    assert argv[offset-1]=='--ro-bind'
    assert '/srv/work/case/investigation-old' not in argv
    for value in ('new','../other','/arbitrary'):
        with pytest.raises(ValueError):sandbox_argv(**(args | {'seed_work_id':value}))


@pytest.mark.parametrize('mutation,expected',[(None,'matched'),('dirty','mismatch'),('head','mismatch'),('unavailable','unavailable')])
def test_candidate_uses_independent_detached_source_probe(conn,config,tmp_path,mutation,expected):
    import json

    from k3_support.project_investigation_observation import observe

    cfg,payload,_job,_case,source=seed(conn,config,tmp_path)
    round_=bugs.start_round(conn,bug_id=payload['bug_id'],actor='owner',request_id='next',reason='Continue',
                            expected_revision=conn.execute('SELECT revision FROM project_bugs').fetchone()[0])
    new=investigation.submit(conn,cfg,payload | {'source':source,'request_id':'next',
        'round_id':round_['round_id'],'expected_revision':conn.execute('SELECT revision FROM project_bugs').fetchone()[0]})
    job=conn.execute('SELECT * FROM jobs WHERE job_id=?',(new['job_id'],)).fetchone()
    conn.execute('INSERT INTO broker_grants VALUES(?,?,?,?,?,?,?,?,?,?,NULL)',
                 ('next-grant','b'*64,1234,job['job_id'],1,job['lifecycle_round'],job['input_digest'],'fixture','now','later'))
    request_id=str(uuid4())
    conn.execute("INSERT INTO broker_remote_actions VALUES(?,1234,'next-grant','fixture','{}','queued','now','now')",(request_id,))
    observed=json.loads(conn.execute('SELECT observation_json FROM project_investigation_checkout_after').fetchone()[0])['source']
    observed['matched']=True
    if mutation=='dirty':observed.update(tracked_content_matches=False,matched=False)
    elif mutation=='head':observed.update(head='c'*40,end_head='c'*40,matched=False)
    calls=[]
    def transport(**kwargs):
        calls.append(kwargs)
        assert 'refs/heads/' not in kwargs['argv'][-1]
        return {'exit_code':1 if mutation=='unavailable' else 0,'stderr':'',
                'stdout':json.dumps({'sources':[observed]})}
    assert observe(conn,cfg,{'request_id':request_id,'grant_id':'next-grant'},transport=transport,heartbeat=lambda:None)==expected
    assert len(calls)==1
    assert conn.execute('SELECT state FROM project_investigation_source_observations').fetchone()[0]==expected
