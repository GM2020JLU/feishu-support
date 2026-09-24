"""Synthetic result review only; no workers, files or Project writes."""

import hashlib
import json
from pathlib import Path

import pytest
from test_broker_results import NOW, result_request
from test_project_bugs import binding

from k3_support import project_bugs
from k3_support.broker_results import submit
from k3_support.project_bug_controls import execute
from k3_support.project_investigation_result import draft


def seed(conn, *, report=True, text=None):
    request = result_request(conn)
    case_id = conn.execute("SELECT case_id FROM jobs WHERE job_id='job-1'").fetchone()[0]
    bug = binding(conn, case_id)
    round_ = project_bugs.start_round(conn, bug_id=bug['bug_id'], actor='owner',
                                     request_id='round', reason='Synthetic', expected_revision=bug['revision'])
    conn.execute("INSERT INTO project_investigation_jobs VALUES('job-1',?)", (round_['round_id'],))
    if text:
        request['params']['result'] = text
    if report:
        submit(conn, request, peer_uid=1234, now=NOW)
    return bug, request


def test_claim_and_execution_do_not_become_verdict(conn, config):
    bug, _ = seed(conn)
    conn.execute("UPDATE jobs SET state='succeeded'")
    before = list(conn.iterdump())
    value = execute(conn, config, action='investigation-result', payload={'bug_id':bug['bug_id'], 'job_id':'job-1'})
    assert value['report']['sections']['status'] == 'completed'
    assert value['report']['source'] == 'worker_claim'
    assert value['execution_state'] == 'succeeded'
    assert value['repair_state'] == 'in_progress'  # investigation exists; no repair-ready verdict
    assert value['verification_state'] == 'not_run'
    assert value['functional_verdict'] == 'not_established'
    assert not value['writeback_available']
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize('valid', [True, False])
def test_artifact_format_is_visible_without_review_or_execution(conn, config, valid):
    bug, request = seed(conn, report=False)
    manifest = {'schema_version': 1, 'repositories': [], 'checks': [], 'requested_actions': []}
    artifacts = json.dumps(manifest) if valid else '{not valid JSON'
    request['params']['result'] = request['params']['result'].replace(
        '## artifacts\nnot verified', '## artifacts\n' + artifacts)
    submit(conn, request, peer_uid=1234, now=NOW)
    before = list(conn.iterdump())
    value = draft(conn, config=config, bug_id=bug['bug_id'], job_id='job-1')
    assert value['report']['artifact_format'] == ('valid' if valid else 'invalid')
    assert value['functional_verdict'] == 'not_established'
    assert value['review'] is None
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize('change', [
    "UPDATE broker_results SET result_text='tampered'",
    "UPDATE cases SET lifecycle_round=lifecycle_round+1",
    "UPDATE jobs SET input_digest='changed'",
    "UPDATE broker_grants SET input_digest='changed'",
])
def test_invalid_report_is_not_displayed(conn, change):
    bug, _ = seed(conn)
    conn.execute(change)
    value = draft(conn, bug_id=bug['bug_id'], job_id='job-1')
    assert value['report'] == {'state':'unavailable'}


@pytest.mark.parametrize('has_report', [True, False])
def test_never_reads_worker_file(conn, monkeypatch, has_report):
    bug, _ = seed(conn, report=has_report)
    def fail(*args):
        raise AssertionError('worker filesystem must not be read')
    monkeypatch.setattr(Path, 'read_bytes', fail)
    value = draft(conn, bug_id=bug['bug_id'], job_id='job-1')
    assert value['report']['state'] == ('available' if has_report else 'not_received')


def test_wrong_bug_job_and_extra_fields_refused(conn, config):
    bug, _ = seed(conn)
    for ids in [('other','job-1'), (bug['bug_id'],'other')]:
        with pytest.raises(ValueError, match='unavailable'):
            draft(conn, bug_id=ids[0], job_id=ids[1])
    with pytest.raises(ValueError, match='exact request'):
        execute(conn, config, action='investigation-result', payload={
            'bug_id':bug['bug_id'], 'job_id':'job-1', 'actor':'other'})


def test_changed_attempt_does_not_reuse_report(conn):
    bug, _ = seed(conn)
    conn.execute('UPDATE jobs SET attempt_no=2')
    assert draft(conn, bug_id=bug['bug_id'], job_id='job-1')['report']['state'] == 'not_received'


def test_limits_sections_but_retains_full_digest(conn):
    bug, request = seed(conn, report=False)
    request['params']['result'] = request['params']['result'].replace('## root_cause\nnot verified', '## root_cause\n'+'界'*5000)
    submit(conn, request, peer_uid=1234, now=NOW)
    report = draft(conn, bug_id=bug['bug_id'], job_id='job-1')['report']
    assert len(report['sections']['root_cause']) == 4000
    assert report['truncated_sections'] == ['root_cause']
    assert report['digest'] == hashlib.sha256(request['params']['result'].encode()).hexdigest()


@pytest.mark.parametrize('digest,state', [('same','verified'), ('different','stale')])
def test_review_digest_is_scoped_and_never_functional_proof(conn, digest, state):
    bug, request = seed(conn)
    report_digest = hashlib.sha256(request['params']['result'].encode()).hexdigest()
    conn.execute("""INSERT INTO codex_reviews(review_id,job_id,case_id,status,result_digest,
                 manifest_json,independent_checks_json,created_at,updated_at)
                 VALUES('review','job-1',?,'verified',?,'{}','[]','now','now')""",
                 (bug['case_id'], report_digest if digest=='same' else '0'*64))
    review = draft(conn, bug_id=bug['bug_id'], job_id='job-1')['review']
    assert review['state'] == state and review['functional_verdict'] == 'not_established'


def test_archive_and_caller_transaction_remain_intact(conn):
    bug, _ = seed(conn)
    conn.execute("UPDATE jobs SET state='succeeded'")
    revision = conn.execute('SELECT revision FROM project_bugs').fetchone()[0]
    project_bugs.start_round(conn, bug_id=bug['bug_id'], actor='owner', request_id='next', reason='Again', expected_revision=revision)
    conn.execute('BEGIN')
    before = list(conn.iterdump())
    assert draft(conn, bug_id=bug['bug_id'], job_id='job-1')['archived']
    assert conn.in_transaction and list(conn.iterdump()) == before
    conn.execute('ROLLBACK')


def test_command_receipts_are_bounded_and_exclude_worker_output(conn):
    bug, _ = seed(conn)
    grant = conn.execute('SELECT grant_id FROM broker_grants').fetchone()[0]
    for index in range(7):
        request = f'request-{index}'
        conn.execute("INSERT INTO broker_remote_actions VALUES(?,1234,?,'digest','{}','succeeded',?,?)",
                     (request, grant, str(index), str(index)))
        conn.execute("INSERT INTO broker_remote_results VALUES(?,0,'PRIVATE STDOUT','PRIVATE STDERR','now')", (request,))
    value = draft(conn, bug_id=bug['bug_id'], job_id='job-1')
    assert len(value['commands']) == 5
    assert value['commands'][0]['request_id'] == 'request-6'
    assert value['commands'][0]['attempt_no'] == value['attempt_no'] == 1
    assert 'PRIVATE' not in json.dumps(value)
    assert value['functional_verdict'] == 'not_established'


def test_parses_digest_checked_report_not_cached_sections(conn):
    bug, _ = seed(conn)
    conn.execute("UPDATE broker_results SET sections_json='{}'")
    value = draft(conn, bug_id=bug['bug_id'], job_id='job-1')
    assert value['report']['sections']['status'] == 'completed'
