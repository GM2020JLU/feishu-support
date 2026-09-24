"""Same-round continuation must review all increments, not just the last job."""
import pytest

from test_project_repair_reviews import setup_evidence,detail,record,review_payload,settle_repository_job
from k3_support import project_investigation as investigation


@pytest.mark.parametrize('parent_evidence',[True,False])
def test_same_repository_continuation_requires_complete_original_baseline_chain(conn,config,tmp_path,parent_evidence):
    cfg,task,parent,_=setup_evidence(conn,config,tmp_path,evidence=parent_evidence)
    original_request=conn.execute('SELECT request_id FROM project_investigation_checkout_after WHERE job_id=?',(parent,)).fetchone()[0]
    source={**task['source'],'base_commit':'b'*40,'candidate_request_id':original_request}
    revision=conn.execute('SELECT revision FROM project_bugs WHERE bug_id=?',(task['bug_id'],)).fetchone()[0]
    child=investigation.submit(conn,cfg,task|{'request_id':'continue-same-repo','source':source,
          'expected_revision':revision,'predecessor_job_id':parent})['job_id']
    conn.execute("UPDATE jobs SET state='succeeded',attempt_no=1 WHERE job_id=?",(child,))
    settle_repository_job(conn,cfg,task,child,head='c'*40)
    view=detail(conn,cfg,task)
    assert view['can_mark_ready'] is parent_evidence
    repo=view['repositories'][0]
    if parent_evidence:
        assert [(s['base_commit'],s['head_commit']) for s in repo['segments']]==[('a'*40,'b'*40),('b'*40,'c'*40)]
        assert all(s['patch_text'] for s in repo['segments'])
        record(conn,cfg,review_payload(view,task))
        assert detail(conn,cfg,task)['repair_state']=='ready'
    else:
        assert 'round_changeset_chain_incomplete' in repo['blockers']
        with pytest.raises(ValueError,match='complete current repository evidence'):
            record(conn,cfg,review_payload(view,task))
