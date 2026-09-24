# ruff: noqa: F811 -- imported fixture
import json
import sqlite3
from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID
from test_project_investigation_source import (  # noqa: F401
    baseline_result,
    checkout_result,
    source_remote,
)

from k3_support.broker_remote_runner import run_one


def after_result(conn, mutation):
    before=checkout_result(conn)
    receipt=json.loads(before['stdout'].split('\n')[0].removeprefix('K3_CHECKOUT_V1:'))
    source=receipt['source'] | mutation
    if 'matched' not in mutation:
        source['matched']=(source['head']==source['end_head']==receipt['base_commit']
                           and source['base_is_ancestor'] and source['tracked_content_matches'])
    return {'exit_code':0,'stdout':json.dumps({'sources':[source]}),'stderr':''}


@pytest.mark.parametrize('mutation,expected',[
    ({},'clean'),({'tracked_content_matches':False},'dirty'),
    ({'head':'b'*40,'end_head':'b'*40},'clean'),
    ({'base_is_ancestor':False},'mismatch'),({'end_head':'b'*40},'mismatch'),
    ({'repository':'other'},'unavailable'),({'path':'/other'},'unavailable'),
    ({'head':'short'},'unavailable'),({'tracked_count':True},'unavailable'),
    ({'tracked_content_matches':1},'unavailable'),({'matched':False},'unavailable'),
])
def test_exit_and_source_observation_are_separate(conn,source_remote,mutation,expected):
    cfg,reader=source_remote
    calls=[]
    def transport(**kw):
        calls.append(kw)
        if len(calls)==1:return baseline_result(cfg)
        if len(calls)==2:return checkout_result(conn)
        return after_result(conn,mutation)
    assert run_one(conn,cfg,contract_reader=reader,transport=transport)['state']=='succeeded'
    assert len(calls)==3
    row=conn.execute('SELECT * FROM project_investigation_checkout_after').fetchone()
    assert row['state']==expected
    assert conn.execute('SELECT exit_code FROM broker_remote_results').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM project_investigation_checkouts').fetchone()[0]==1
    assert 'verification_source_probe' not in calls[2]['argv'][-1]  # fixed program sent inline, not a worker path
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        conn.execute("UPDATE project_investigation_checkout_after SET state='clean'")


@pytest.mark.parametrize('mode',['timeout','nonzero','oversize','invalid_json'])
def test_sampler_failure_keeps_real_command_exit(conn,source_remote,mode):
    cfg,reader=source_remote
    calls=[]
    def transport(**kw):
        calls.append(True)
        if len(calls)==1:return baseline_result(cfg)
        if len(calls)==2:return checkout_result(conn) | {'exit_code':7}
        if mode=='timeout':raise OSError('private endpoint details')
        return {'exit_code':1 if mode=='nonzero' else 0,'stdout':'x'*(2*1024*1024+1) if mode=='oversize' else 'private','stderr':'private'}
    assert run_one(conn,cfg,contract_reader=reader,transport=transport)['state']=='failed'
    assert conn.execute('SELECT exit_code FROM broker_remote_results').fetchone()[0]==7
    row=conn.execute('SELECT observation_json FROM project_investigation_checkout_after').fetchone()
    assert json.loads(row[0])=={'state':'unavailable','source':None}


@pytest.mark.parametrize('exit_code',[124,125,255,-9])
def test_unknown_command_never_triggers_post_sampling(conn,source_remote,exit_code):
    cfg,reader=source_remote
    calls=[]
    def transport(**kw):
        calls.append(True)
        return baseline_result(cfg) if len(calls)==1 else checkout_result(conn) | {'exit_code':exit_code}
    assert run_one(conn,cfg,contract_reader=reader,transport=transport)['state']=='unknown'
    assert len(calls)==2
    assert conn.execute('SELECT count(*) FROM project_investigation_checkout_after').fetchone()[0]==0


def test_source_observation_is_in_authenticated_command_read(conn,source_remote):
    from k3_support.broker_claim_receipts import claim
    from k3_support.broker_remote import read
    cfg,reader=source_remote
    calls=[]
    def transport(**kw):
        calls.append(True)
        return baseline_result(cfg) if len(calls)==1 else checkout_result(conn) if len(calls)==2 else after_result(conn,{})
    run_one(conn,cfg,contract_reader=reader,transport=transport)
    claim_id=conn.execute('SELECT request_id FROM broker_claim_receipts').fetchone()[0]
    task=claim(conn,cfg,{'version':1,'request_id':claim_id,'method':'claim','params':{'pool':'debug'}},peer_uid=UID,control_key=b't'*32,now=NOW)['task']
    request_id=conn.execute('SELECT request_id FROM broker_remote_actions').fetchone()[0]
    query={'version':1,'request_id':str(uuid4()),'method':'remote_read','params':{**task,'remote_request_id':request_id,'offset':0}}
    result=read(conn,cfg,query,peer_uid=UID,contract_reader=reader,now=NOW)
    assert result['checkout_after']['state']=='clean' and result['stdout']=='synthetic'
    with pytest.raises(ValueError):
        read(conn,cfg,query,peer_uid=UID+1,contract_reader=reader,now=NOW)


@pytest.mark.parametrize('tampered',[False,True])
def test_complete_changeset_is_bound_and_persisted_without_changing_exit(conn,source_remote,tampered):
    import base64
    import hashlib

    cfg,reader=source_remote
    calls=[]
    def transport(**kw):
        calls.append(True)
        if len(calls)==1:return baseline_result(cfg)
        if len(calls)==2:return checkout_result(conn)
        result=after_result(conn,{'head':'b'*40,'end_head':'b'*40})
        value=json.loads(result['stdout'])
        before=json.loads(checkout_result(conn)['stdout'].split('\n')[0].removeprefix('K3_CHECKOUT_V1:'))
        patch=b'fixture binary patch'
        value['changeset']={'state':'observed','coverage':'complete_committed_changeset_v1',
             'base_commit':before['base_commit'],'head_commit':'b'*40,'commits':['b'*40],
             'patch_b64':base64.b64encode(patch).decode(),'patch_sha256':'wrong' if tampered else hashlib.sha256(patch).hexdigest(),
             'paths_b64':base64.b64encode(b'file\0').decode(),'untracked_paths_b64':'','tracked_content_matches':True}
        return result|{'stdout':json.dumps(value)}
    assert run_one(conn,cfg,contract_reader=reader,transport=transport)['state']=='succeeded'
    value=json.loads(conn.execute('SELECT observation_json FROM project_investigation_checkout_after').fetchone()[0])
    assert ('changeset' in value) is not tampered
    if not tampered:assert value['changeset']['commits']==['b'*40]
    assert conn.execute('SELECT exit_code FROM broker_remote_results').fetchone()[0]==0
