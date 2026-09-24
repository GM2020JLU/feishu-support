import json
import sqlite3
from datetime import timedelta

import pytest
from test_investigation_result import seed
from test_project_bugs import observe

from k3_support import project_bug_grants as grants
from k3_support import project_bug_operations as operations
from k3_support import project_result_comment as comments
from k3_support.project_investigation_result import draft
from k3_support.timeutil import utc_now


def setup(conn):
    bug, _ = seed(conn)
    snapshot = observe(conn, bug)
    scope = {key:bug[key] for key in ('host','project_key','type_key')}
    scope.update(bug_ids=[bug['bug_id']], actions=['bug.comment'], fields=[], transitions=[], repositories=[], devices=[])
    grant = grants.issue(conn, actor='owner', request_id='grant', scope=scope,
                         expires_at=(utc_now()+timedelta(hours=1)).isoformat())
    result = draft(conn, bug_id=bug['bug_id'], job_id='job-1')
    return {'bug_id':bug['bug_id'], 'job_id':'job-1', 'result_digest':result['report']['digest'],
            'expected_revision':result['bug_revision'], 'snapshot_id':snapshot['snapshot_id'],
            'grant_id':grant['grant_id'], 'request_id':'comment', 'text':'Reviewed investigation, not verified.'}


def test_prepare_and_retry_are_atomic_local_only(conn):
    payload = setup(conn)
    first = comments.prepare(conn, actor='owner', payload=payload)
    assert first['state']=='prepared' and first['write_json'] is None
    assert json.loads(first['change_json'])=={'text':payload['text']}
    assert conn.execute('SELECT count(*) FROM project_result_comments').fetchone()[0]==1
    before=list(conn.iterdump())
    assert comments.prepare(conn, actor='owner', payload=payload)==first
    assert list(conn.iterdump())==before
    assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0]==0
    assert conn.execute('SELECT repair_state,verification_state FROM project_bug_rounds').fetchone()[:]==('not_started','not_run')


@pytest.mark.parametrize('key,value', [('text','changed'),('job_id','other'),('result_digest','0'*64),('grant_id','other')])
def test_reused_request_never_changes_provenance(conn,key,value):
    payload=setup(conn)
    comments.prepare(conn,actor='owner',payload=payload)
    with pytest.raises(ValueError,match='reused'):
        comments.prepare(conn,actor='owner',payload=payload|{key:value})


@pytest.mark.parametrize('key,value', [('result_digest','0'*64),('snapshot_id','wrong'),('expected_revision',0),('job_id','other')])
def test_stale_or_wrong_source_stages_nothing(conn,key,value):
    payload=setup(conn)
    with pytest.raises(ValueError):
        comments.prepare(conn,actor='owner',payload=payload|{key:value})
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0]==0


def test_revocation_refuses_new_prepare(conn):
    payload=setup(conn)
    grants.revoke(conn,actor='owner',grant_id=payload['grant_id'])
    with pytest.raises(PermissionError):
        comments.prepare(conn,actor='owner',payload=payload)


def test_changed_report_is_rechecked_before_dispatch_but_not_on_replay(conn):
    payload=setup(conn)
    operation=comments.prepare(conn,actor='owner',payload=payload)
    comments.guard(conn,operation)
    conn.execute("UPDATE jobs SET attempt_no=2")
    with pytest.raises(ValueError,match='result changed'):
        comments.guard(conn,operation)
    with pytest.raises(ValueError,match='result changed'):
        operations._guard(conn,None,operation)
    assert comments.prepare(conn,actor='owner',payload=payload)==operation


def test_link_failure_rolls_back_operation_and_event(conn):
    payload=setup(conn)
    conn.execute("CREATE TRIGGER reject_provenance BEFORE INSERT ON project_result_comments BEGIN SELECT RAISE(ABORT,'injected'); END")
    before=list(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError,match='injected'):
        comments.prepare(conn,actor='owner',payload=payload)
    assert list(conn.iterdump())==before


def test_caller_rollback_and_immutable_link(conn):
    payload=setup(conn)
    conn.execute('BEGIN')
    comments.prepare(conn,actor='owner',payload=payload)
    assert conn.in_transaction
    conn.execute('ROLLBACK')
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0]==0
    comments.prepare(conn,actor='owner',payload=payload)
    for sql in ["UPDATE project_result_comments SET result_digest='changed'", 'DELETE FROM project_result_comments']:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)


def test_generic_request_id_cannot_be_rebound_to_result(conn):
    payload=setup(conn)
    operations.prepare(conn,actor='owner', action='bug.comment', change={'text':payload['text']},
                       **{key:payload[key] for key in ('bug_id','snapshot_id','expected_revision','grant_id','request_id')})
    with pytest.raises(ValueError,match='reused'):
        comments.prepare(conn,actor='owner',payload=payload)


def test_authenticated_route_owns_actor_and_rejects_extra_parameters(conn,config):
    import copy

    from k3_support.config import Config
    from k3_support.project_bug_controls import execute

    payload=setup(conn)
    raw=copy.deepcopy(config.raw)
    raw['identity']['control_operator_id']='owner'
    cfg=Config(raw,config.path)
    with pytest.raises(ValueError,match='exact request'):
        execute(conn,cfg,action='prepare-result-comment',payload=payload|{'actor':'other'})
    result=execute(conn,cfg,action='prepare-result-comment',payload=payload)
    assert result['actor']=='owner'
