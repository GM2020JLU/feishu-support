# ruff: noqa: F811 -- imported fixture
import json

import pytest
from test_project_investigation_source import (  # noqa: F401
    baseline_result,
    checkout_result,
    source_remote,
)

from k3_support.broker_remote_runner import run_one


@pytest.mark.parametrize('mutation,expected',[
    ({},'succeeded'),({'base_is_ancestor':False},'cancelled'),
    ({'base_commit':'c'*40},'cancelled'),({'end_branch_commit':'c'*40},'cancelled'),
    ({'path':'/other'},'cancelled'),({'base_is_ancestor':1},'cancelled'),
    ({'base_commit':'short'},'cancelled'),({'extra':'model-claim'},'cancelled'),
])
def test_control_probe_gates_business_command(conn,source_remote,mutation,expected):
    cfg,reader=source_remote
    calls=[]
    def transport(**kw):
        calls.append(kw)
        if len(calls)==1:
            result=baseline_result(cfg)
            result['stdout']=json.dumps(json.loads(result['stdout']) | mutation)
            return result
        return checkout_result(conn)
    result=run_one(conn,cfg,contract_reader=reader,transport=transport)
    assert result['state']==expected
    assert len(calls)==(3 if expected=='succeeded' else 1)
    observation=conn.execute('SELECT * FROM project_investigation_source_observations').fetchone()
    assert observation is not None
    assert observation['state']==('matched' if not mutation else 'mismatch' if next(iter(mutation)) in {'base_commit','end_branch_commit','base_is_ancestor'} and mutation != {'base_is_ancestor':1} and mutation != {'base_commit':'short'} else 'unavailable')
    assert '-I' in calls[0]['argv'][-1] and '-S' in calls[0]['argv'][-1]
    assert conn.execute('SELECT count(*) FROM broker_remote_results').fetchone()[0]==(1 if expected=='succeeded' else 0)


@pytest.mark.parametrize('mode',['nonzero','timeout','invalid_json','oversize'])
def test_failed_probe_retains_safe_reason_and_never_runs_command(conn,source_remote,mode):
    cfg,reader=source_remote
    calls=[]
    def transport(**kw):
        calls.append(True)
        if mode=='timeout':raise OSError('sensitive remote details')
        return {'exit_code':1 if mode=='nonzero' else 0,
                'stdout':'x'*16001 if mode=='oversize' else 'private malformed result','stderr':'private'}
    assert run_one(conn,cfg,contract_reader=reader,transport=transport)['state']=='cancelled'
    assert len(calls)==1
    row=conn.execute('SELECT * FROM project_investigation_source_observations').fetchone()
    assert row['state']=='unavailable'
    assert json.loads(row['observation_json'])=={'reason':'baseline_observation_unavailable'}
    with pytest.raises(Exception,match='immutable'):
        conn.execute("UPDATE project_investigation_source_observations SET state='matched'")
