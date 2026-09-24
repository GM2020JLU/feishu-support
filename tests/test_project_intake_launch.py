import pytest
from test_coding_catalog import configure
from test_executors import executor_config
from test_project_bugs import binding

from k3_support import project_bugs as bugs
from k3_support.coding_catalog import choices
from k3_support.project_investigation import submit
from k3_support.project_investigation_source import selection
from k3_support.store import create_case


def setup(conn,config,tmp_path):
    cfg=executor_config(config,codex=True)
    configure(cfg,tmp_path,'codex')
    case,_=create_case(conn,title='Imported Bug',case_type='bug',severity='P2',confidence=1)
    bug=binding(conn,case)
    round_=bugs.start_round(conn,bug_id=bug['bug_id'],actor=cfg.control_operator_id,request_id='round',reason='Investigate',expected_revision=1)
    payload={'bug_id':bug['bug_id'],'round_id':round_['round_id'],'expected_revision':2,
             'case_version':1,'repository':'u-boot','executor_id':'primary',
             'contract_fingerprint':choices(cfg)['items'][0]['contract_fingerprint'],
             'instructions':'Investigate','acceptance':'Regression check','request_id':'launch',
             'source':{**selection(cfg,'u-boot'),'branch':'main','base_commit':'a'*40,'version':''}}
    return cfg,case,payload


def test_explicit_launch_makes_imported_case_dispatchable_once(conn,config,tmp_path):
    cfg,case,payload=setup(conn,config,tmp_path)
    assert conn.execute('SELECT state FROM cases WHERE case_id=?',(case,)).fetchone()[0]=='intake'
    first=submit(conn,cfg,payload)
    assert first['created']
    row=conn.execute('SELECT state,version FROM cases WHERE case_id=?',(case,)).fetchone()
    assert tuple(row)==('triage',2)
    assert not submit(conn,cfg,payload)['created']
    assert conn.execute("SELECT count(*) FROM case_events WHERE idempotency_key LIKE 'bug-launch:%'").fetchone()[0]==1
    from k3_support.broker_queue import next_candidate
    from k3_support.coding_catalog import resolve
    from k3_support.db import transaction
    from k3_support.timeutil import utc_now
    with transaction(conn):
        candidate=next_candidate(conn,now=utc_now(),contract=resolve(cfg,'primary',expected_fingerprint=payload['contract_fingerprint']))
    assert candidate['job_id']==first['job_id']
    assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0]==0


def test_failed_binding_rolls_back_case_transition_and_job(conn,config,tmp_path):
    cfg,case,payload=setup(conn,config,tmp_path)
    conn.execute("CREATE TEMP TRIGGER reject_round_job BEFORE INSERT ON project_investigation_jobs BEGIN SELECT RAISE(ABORT,'injected link failure'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError,match='injected link failure'):
        submit(conn,cfg,payload)
    assert tuple(conn.execute('SELECT state,version FROM cases WHERE case_id=?',(case,)).fetchone())==('intake',1)
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    assert conn.execute("SELECT count(*) FROM case_events WHERE idempotency_key LIKE 'bug-launch:%'").fetchone()[0]==0


@pytest.mark.parametrize('state',['paused','takeover','escalated','error','answering'])
def test_launch_never_implicitly_resumes_other_states(conn,config,tmp_path,state):
    cfg,case,payload=setup(conn,config,tmp_path)
    conn.execute('UPDATE cases SET state=? WHERE case_id=?',(state,case))
    with pytest.raises(bugs.BugConflict,match='explicitly resumed'):
        submit(conn,cfg,payload)
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
