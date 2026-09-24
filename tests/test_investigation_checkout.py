# ruff: noqa: F811 -- imported fixture
import json

import pytest
from test_project_investigation_source import (  # noqa: F401
    baseline_result,
    checkout_result,
    source_remote,
)

from k3_support.broker_remote_runner import run_one


@pytest.mark.parametrize('mutation',['none','missing','wrong_job','wrong_root','dirty_initial','ancestor_false','head_changed','malformed'])
def test_checkout_frame_is_required_and_exact(conn,source_remote,mutation):
    cfg,reader=source_remote
    def transport(**kw):
        if 'source_repository_baseline' in kw['argv'][-1]:
            return baseline_result(cfg)
        result=checkout_result(conn)
        prefix,rest=result['stdout'].split('\n',1)
        value=json.loads(prefix.removeprefix('K3_CHECKOUT_V1:'))
        if mutation=='missing':result['stdout']=rest
        elif mutation=='malformed':result['stdout']='K3_CHECKOUT_V1:{broken}\n'+rest
        else:
            if mutation=='wrong_job':value['job_id']='other-job'
            if mutation=='wrong_root':value['source']['path']='/other/repository'
            if mutation=='dirty_initial':value['source'].update(tracked_content_matches=False,matched=False)
            if mutation=='ancestor_false':value['source']['base_is_ancestor']=False
            if mutation=='head_changed':value['source']['end_head']='b'*40
            result['stdout']='K3_CHECKOUT_V1:'+json.dumps(value)+'\n'+rest
        return result
    if mutation=='none':
        assert run_one(conn,cfg,contract_reader=reader,transport=transport)['state']=='succeeded'
        assert conn.execute('SELECT stdout FROM broker_remote_results').fetchone()[0]=='synthetic'
        assert conn.execute('SELECT count(*) FROM project_investigation_checkouts').fetchone()[0]==1
    else:
        with pytest.raises(ValueError):
            run_one(conn,cfg,contract_reader=reader,transport=transport)
        assert conn.execute('SELECT state FROM broker_remote_actions').fetchone()[0]=='unknown'
        assert conn.execute('SELECT count(*) FROM project_investigation_checkouts').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM broker_remote_results').fetchone()[0]==0


def test_preparation_failure_does_not_invent_checkout(conn,source_remote):
    cfg,reader=source_remote
    def transport(**kw):
        if 'source_repository_baseline' in kw['argv'][-1]:return baseline_result(cfg)
        return {'exit_code':126,'stdout':'','stderr':'existing files preserved'}
    assert run_one(conn,cfg,contract_reader=reader,transport=transport)['state']=='failed'
    assert conn.execute('SELECT count(*) FROM project_investigation_checkouts').fetchone()[0]==0


def test_plan_mounts_only_private_job_child(conn,source_remote):
    cfg,_=source_remote
    plan=json.loads(conn.execute('SELECT plan_json FROM broker_remote_actions').fetchone()[0])
    binding=plan['investigation_checkout']
    assert binding['root'].endswith('/investigation-job-1')
    assert binding['continuation'] is False
    assert '--bind '+binding['root']+' '+binding['root'] in plan['command']
    assert '--no-hardlinks' in plan['command'] and '--dissociate' in plan['command']
    assert 'initial_checkout_not_clean_baseline' in plan['command']
    assert str(cfg.runtime('remote_worktree_root')) in binding['root']


def test_checkout_receipt_rolls_back_when_result_commit_fails(conn,source_remote):
    import sqlite3

    cfg,reader=source_remote
    conn.execute("CREATE TEMP TRIGGER reject_remote_receipt BEFORE INSERT ON broker_remote_results BEGIN SELECT RAISE(ABORT,'injected result failure'); END")
    def transport(**kw):
        return baseline_result(cfg) if 'source_repository_baseline' in kw['argv'][-1] else checkout_result(conn)
    with pytest.raises(sqlite3.IntegrityError,match='injected result failure'):
        run_one(conn,cfg,contract_reader=reader,transport=transport)
    assert conn.execute('SELECT count(*) FROM project_investigation_checkouts').fetchone()[0]==0
    assert conn.execute('SELECT state FROM broker_remote_actions').fetchone()[0]=='unknown'
    assert conn.execute('SELECT count(*) FROM project_investigation_checkout_after').fetchone()[0]==0
