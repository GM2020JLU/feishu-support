"""Resolve a prior control-observed commit, never a caller-supplied seed path.

A candidate is source provenance only. It conveys neither a test verdict nor
permission to reuse the predecessor's execution grant or writable workspace.
"""

import json
import re
from pathlib import PurePosixPath

from .ids import digest
from .project_investigation_source import require_current, validate
from .project_round_readiness import require as require_settled


def resolve(conn, config, *, case_id, repository, source):
    validate(source)
    request_id = source.get('candidate_request_id')
    if request_id is None:
        return None
    if conn is None:
        raise ValueError('candidate control evidence unavailable')
    row = conn.execute(
        """SELECT o.*,j.case_id,j.state AS job_state,j.attempt_no,j.input_digest,j.lifecycle_round,
           i.round_id,g.attempt_no AS grant_attempt,g.input_digest AS grant_input,
           g.lifecycle_round AS grant_round,a.state AS action_state,a.plan_json,r.exit_code,
           b.payload_json FROM project_investigation_checkout_after o
           JOIN jobs j USING(job_id) JOIN project_investigation_jobs i USING(job_id)
           JOIN broker_remote_actions a USING(request_id) JOIN broker_grants g ON g.grant_id=a.grant_id
           JOIN broker_remote_results r USING(request_id) JOIN broker_inputs b ON b.job_id=j.job_id
           JOIN broker_execution_starts started ON started.grant_id=g.grant_id
           WHERE o.request_id=? AND g.job_id=j.job_id""", (request_id,),
    ).fetchone()
    if (row is None or row['case_id'] != case_id or row['state'] != 'clean'
            or row['job_state'] not in {'succeeded', 'failed', 'cancelled'}
            or row['attempt_no'] != row['grant_attempt'] or row['input_digest'] != row['grant_input']
            or row['lifecycle_round'] != row['grant_round']
            or not ((row['action_state'] == 'succeeded' and row['exit_code'] == 0)
                    or (row['action_state'] == 'failed' and 1 <= row['exit_code'] <= 254
                        and row['exit_code'] not in (124, 125)))):
        raise ValueError('candidate source is not settled control evidence')
    latest = conn.execute(
        'SELECT a.request_id FROM broker_remote_actions a JOIN broker_grants g USING(grant_id) '
        'WHERE g.job_id=? ORDER BY a.rowid DESC LIMIT 1', (row['job_id'],),
    ).fetchone()
    if latest is None or latest['request_id'] != request_id:
        raise ValueError('candidate was superseded by a later command')
    require_settled(conn, row['round_id'], job_id=row['job_id'])
    from .content_retirement import require_case_content

    require_case_content(conn, case_id=case_id, lifecycle_round=row['lifecycle_round'])
    try:
        payload = json.loads(row['payload_json'])
        parent = payload['context_extra']['project_investigation']['source']
        if (digest(payload) != row['input_digest'] or payload['repos'] != [repository]
                or payload['case_id'] != case_id or payload['lifecycle_round'] != row['lifecycle_round']):
            raise ValueError('candidate input binding changed')
        require_current(config, repository, parent)
        require_current(config, repository, source)
        if any(source[key] != parent[key] for key in ('branch', 'node', 'deployment_fingerprint')):
            raise ValueError('candidate repository selection changed')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', row['job_id']):
            raise ValueError('candidate job identity invalid')
        path = str(PurePosixPath(config.runtime('remote_worktree_root')) / case_id
                   / ('investigation-' + row['job_id']) / 'repository')
        observation = json.loads(row['observation_json'])
        actual = observation['source']
        plan = json.loads(row['plan_json'])['investigation_checkout']
        if (observation['state'] != 'clean' or actual['repository'] != repository
                or actual['path'] != path or actual['head'] != source['base_commit']
                or actual['end_head'] != source['base_commit']
                or actual['base_is_ancestor'] is not True or actual['tracked_content_matches'] is not True
                or actual['coverage'] != 'tracked_source_sample'
                or plan['job_id'] != row['job_id'] or plan['request_id'] != request_id
                or plan['root'] + '/repository' != path or plan['repository'] != repository
                or plan['base_commit'] != parent['base_commit']):
            raise ValueError('candidate observation binding changed')
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError('candidate evidence unavailable') from None
    return {'job_id': row['job_id'], 'request_id': request_id, 'path': path,
            'repository': repository, 'source': source}


def choices(conn, config, case_id):
    """Bounded hints only; submit, dispatch and checkout revalidate the choice."""
    result = []
    rows = conn.execute(
        """SELECT o.request_id,o.observation_json,b.payload_json FROM project_investigation_checkout_after o
           JOIN jobs j USING(job_id) JOIN broker_inputs b USING(job_id)
           WHERE j.case_id=? AND o.state='clean' ORDER BY o.rowid DESC LIMIT 100""", (case_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row['payload_json'])
            source = {**payload['context_extra']['project_investigation']['source'],
                      'base_commit': json.loads(row['observation_json'])['source']['head'],
                      'candidate_request_id': row['request_id']}
            candidate = resolve(conn, config, case_id=case_id, repository=payload['repos'][0], source=source)
            result.append({key: value for key, value in candidate.items() if key != 'path'})
        except (ValueError, KeyError, TypeError, IndexError):
            continue
    return result
