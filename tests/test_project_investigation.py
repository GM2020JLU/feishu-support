import json
from pathlib import Path

import pytest
from test_coding_tasks import request_fixture
from test_project_bugs import binding

from k3_support import project_bugs as bugs
from k3_support import project_investigation as investigation


def setup(conn, config, tmp_path, agent='codex'):
    cfg, request = request_fixture(conn, config, tmp_path, agent)
    bug = binding(conn, request['case_id'])
    round_ = bugs.start_round(conn, bug_id=bug['bug_id'], actor=cfg.control_operator_id,
                             request_id='round', reason='Investigate', expected_revision=bug['revision'])
    payload = {k: v for k, v in request.items() if k != 'case_id'}
    from k3_support.project_investigation_source import selection
    payload['source'] = {**selection(cfg,request['repository']), 'branch':'main',
                         'base_commit':'a'*40, 'version':'synthetic'}
    payload.update(bug_id=bug['bug_id'], round_id=round_['round_id'],
                   expected_revision=bug['revision']+1)
    return cfg, payload


@pytest.mark.parametrize('agent', ['codex','claude','dsh','opencode','hermes'])
def test_launch_and_retry_bound_to_round(conn, config, tmp_path, agent):
    cfg, payload = setup(conn, config, tmp_path, agent)
    result = investigation.submit(conn, cfg, payload)
    assert result['created']
    assert not investigation.submit(conn, cfg, payload)['created']
    jobs = investigation.projection(conn, payload['bug_id'])
    assert len(jobs) == 1 and jobs[0]['round_id'] == payload['round_id']
    assert jobs[0]['agent'] == agent
    context = json.loads(conn.execute('SELECT context_json FROM jobs').fetchone()[0])
    assert context['project_investigation']['round_id'] == payload['round_id']
    brief = Path(conn.execute('SELECT workdir FROM jobs WHERE job_id=?',
                              (result['job_id'],)).fetchone()[0], 'brief.md').read_text()
    assert "Put test logs under an evidence directory there, outside the Git checkout" in brief
    assert "inspect Git status including untracked files" in brief
    assert "git ls-files --others (without --exclude-standard)" in brief
    assert "Clean disposable generated files such as Python __pycache__" in brief
    assert conn.execute('SELECT execution_state FROM project_bug_rounds').fetchone()[0] == 'running'
    with pytest.raises(ValueError, match='different content'):
        investigation.submit(conn, cfg, payload | {'instructions': 'changed'})


def test_intake_investigation_is_admitted_and_atomically_triaged(conn, config, tmp_path):
    from k3_support.coding_tasks import options
    cfg, payload = setup(conn, config, tmp_path)
    case_id = conn.execute('SELECT case_id FROM project_bugs WHERE bug_id=?',
                           (payload['bug_id'],)).fetchone()[0]
    conn.execute("UPDATE cases SET state='intake' WHERE case_id=?", (case_id,))
    view = options(conn, cfg, {'case_id': case_id})
    assert not view['submission_allowed']
    assert view['investigation_submission_allowed']
    result = investigation.submit(conn, cfg, payload)
    assert result['created']
    assert conn.execute('SELECT state FROM cases WHERE case_id=?', (case_id,)).fetchone()[0] == 'triage'
    assert len(investigation.projection(conn, payload['bug_id'])) == 1


@pytest.mark.parametrize('state,expected', [('queued','running'),('running','running'),
    ('waiting','blocked'),('orphaned','unknown'),('failed','failed'),
    ('cancelled','cancelled'),('succeeded','succeeded')])
def test_job_state_is_not_repair_or_verification(conn, config, tmp_path, state, expected):
    cfg, payload = setup(conn, config, tmp_path)
    result = investigation.submit(conn, cfg, payload)
    conn.execute('UPDATE jobs SET state=? WHERE job_id=?', (state,result['job_id']))
    round_ = bugs.detail(conn, payload['bug_id'])['rounds'][0]
    assert round_['execution_state'] == expected
    assert round_['repair_state'] == 'not_started'
    assert round_['verification_state'] == 'not_run'


@pytest.mark.parametrize('field,value', [('expected_revision',0),('round_id','wrong'),('expected_revision',True)])
def test_bad_binding_creates_no_job(conn, config, tmp_path, field, value):
    cfg, payload = setup(conn, config, tmp_path)
    with pytest.raises(ValueError):
        investigation.submit(conn, cfg, payload | {field:value})
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_multiple_jobs_must_all_finish(conn, config, tmp_path):
    cfg, payload = setup(conn, config, tmp_path)
    one = investigation.submit(conn, cfg, payload)
    payload = payload | {'request_id':'second','expected_revision':payload['expected_revision']+1}
    two = investigation.submit(conn, cfg, payload)
    conn.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (one['job_id'],))
    assert bugs.detail(conn,payload['bug_id'])['rounds'][0]['execution_state']=='running'
    conn.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (two['job_id'],))
    assert bugs.detail(conn,payload['bug_id'])['rounds'][0]['execution_state']=='failed'


def test_late_revision_change_leaves_no_job(conn, config, tmp_path, monkeypatch):
    from pathlib import Path
    cfg, payload = setup(conn, config, tmp_path)
    original = Path.write_text
    def write(path, *args, **kwargs):
        result = original(path,*args,**kwargs)
        if path.name == '.capability':
            conn.execute('UPDATE project_bugs SET revision=revision+1')
        return result
    monkeypatch.setattr(Path,'write_text',write)
    with pytest.raises(bugs.BugConflict):
        investigation.submit(conn,cfg,payload)
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM project_investigation_jobs').fetchone()[0]==0
    assert not list(tmp_path.rglob('.capability'))


def test_binding_failure_rolls_back_job_and_input(conn, config, tmp_path, monkeypatch):
    cfg, payload = setup(conn, config, tmp_path)
    def fail(*args):
        raise RuntimeError('injected receipt failure')
    monkeypatch.setattr(investigation, 'record', fail)
    with pytest.raises(RuntimeError):
        investigation.submit(conn,cfg,payload)
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM broker_inputs').fetchone()[0]==0
    assert bugs.detail(conn,payload['bug_id'])['rounds'][0]['execution_state']=='planned'


def test_generic_coding_cannot_bypass_round_binding(conn, config, tmp_path):
    from k3_support.coding_tasks import submit
    from k3_support.executors import ExecutorError
    cfg,payload = setup(conn,config,tmp_path)
    case_id=bugs.detail(conn,payload['bug_id'])['case_id']
    generic={k:v for k,v in payload.items() if k not in {'bug_id','round_id','expected_revision','source'}}
    with pytest.raises(ExecutorError,match='investigation entry'):
        submit(conn,cfg,generic | {'case_id':case_id})
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


def test_archived_round_refuses_new_job_but_replays_original(conn, config, tmp_path):
    cfg,payload=setup(conn,config,tmp_path)
    one=investigation.submit(conn,cfg,payload)
    conn.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?",(one['job_id'],))
    revision=bugs.detail(conn,payload['bug_id'])['revision']
    bugs.start_round(conn,bug_id=payload['bug_id'],actor=cfg.control_operator_id,
                     request_id='new-round',reason='Reopened investigation',expected_revision=revision)
    assert not investigation.submit(conn,cfg,payload)['created']
    with pytest.raises(bugs.BugConflict,match='archived'):
        investigation.submit(conn,cfg,payload | {'request_id':'late','expected_revision':revision+1})


def test_unsettled_verification_prevents_new_coding(conn, config, tmp_path, monkeypatch):
    from k3_support import project_verification_runs
    cfg,payload=setup(conn,config,tmp_path)
    monkeypatch.setattr(project_verification_runs,'unsettled',lambda *args:True)
    with pytest.raises(bugs.BugConflict,match='verification execution'):
        investigation.submit(conn,cfg,payload)
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


def test_control_route_uses_server_actor_and_exact_fields(conn, config, tmp_path):
    from k3_support.project_bug_controls import execute
    cfg,payload=setup(conn,config,tmp_path)
    with pytest.raises(ValueError,match='exact request'):
        execute(conn,cfg,action='create-investigation-job',payload=payload | {'actor':'intruder'})
    result=execute(conn,cfg,action='create-investigation-job',payload=payload)
    detail=execute(conn,cfg,action='detail',payload={'bug_id':payload['bug_id']})
    assert detail['investigation_jobs'][0]['job_id']==result['job_id']
    row=conn.execute('SELECT context_json,workdir FROM jobs').fetchone()
    assert json.loads(row['context_json'])['project_investigation']['actor']==cfg.control_operator_id
    from pathlib import Path
    brief=Path(row['workdir'],'brief.md').read_text()
    assert payload['round_id'] in brief and 'does not establish repair completion' in brief


def test_observed_reopen_rejects_new_job_until_new_round(conn, config, tmp_path):
    from test_project_close_lifecycle import observe, writer

    cfg, payload = setup(conn, config, tmp_path)
    writer(cfg)
    bug = bugs._bug(conn, payload['bug_id'])
    observe(conn, bug, 'OPEN', 'admission-open')
    observe(conn, bug, 'CLOSED', 'admission-close')
    observe(conn, bug, 'OPEN', 'admission-reopen')
    payload['expected_revision'] = bugs._bug(conn, payload['bug_id'])['revision']
    with pytest.raises(bugs.BugConflict, match='reopened'):
        investigation.submit(conn, cfg, payload)
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
    new = bugs.start_round(conn, bug_id=payload['bug_id'], actor=cfg.control_operator_id,
                          request_id='reopened-new-round', reason='Investigate recurrence',
                          expected_revision=payload['expected_revision'])
    result = investigation.submit(conn, cfg, payload | {
        'round_id': new['round_id'],
        'expected_revision': bugs._bug(conn, payload['bug_id'])['revision']})
    assert result['created']
    assert bugs.detail(conn, payload['bug_id'])['rounds'][0]['verification_state'] == 'not_run'
