"""Journal a checkout-only action before verification source sampling.

Both actions use the existing broker grant, heartbeat, sandbox, receipt and
unknown-result recovery. This module never executes a command in the listener.
"""

import hashlib
import json
from copy import deepcopy
from uuid import uuid4

from .codex_remote import build_remote_command
from .ids import canonical_json, digest
from .project_investigation_checkout import prepare, companion_seeds
from .remote_guard import receipt_directory, wrap
from .timeutil import iso_now


def enqueue(conn, config, *, inputs, action, case_id):
    plan = json.loads(action['plan_json'])
    checkout = plan.get('investigation_checkout')
    if checkout is None or 'verification_run_id' not in plan:
        return
    # Evidence must describe the very checkout in which the test will execute.
    sources = conn.execute('SELECT bindings_json FROM project_verification_sources WHERE run_id=?',
                           (plan['verification_run_id'],)).fetchone()
    bindings = json.loads(sources['bindings_json']) if sources else []
    expected = {checkout['repository']: (checkout['root'] + '/repository', checkout['base_commit'])}
    for item in checkout.get('companions', []):
        if item['repository'] in expected:
            raise ValueError('duplicate joint source binding')
        expected[item['repository']] = (checkout['root'] + '/' + item['relative_path'], item['base_commit'])
    if (len(bindings) != len(expected) or len({b['repository'] for b in bindings}) != len(bindings)
            or any(expected.get(b['repository']) != (b['path'], b['candidate_commit']) for b in bindings)):
        raise ValueError('verification requires the exact isolated candidate source binding')
    request_id = str(uuid4())
    command, binding = prepare(conn, config, inputs, job_id=checkout['job_id'], case_id=case_id,
                               request_id=request_id, remote={'mode':'work','repo':checkout['repository'],'command':':'})
    from .config import Config

    raw = deepcopy(config.raw)
    raw['repositories'] = {name: raw['repositories'][name] for name in inputs['repos']}
    rendered = build_remote_command(Config(raw, config.path), case_id=case_id, mode='work', repo_name=checkout['repository'],
                                    command=command, work_id=checkout['job_id'], seed_work_id=binding.get('seed_job_id'),
                                    seed_work_ids=companion_seeds(binding))
    directory = receipt_directory(config.raw['runtime'])
    journal = {'receipt_directory':directory,'request_id':request_id} if directory else {}
    stored = {key:plan[key] for key in ('guard_version','input_digest','transport','host','ssh_command','contract_digest')}
    stored.update(command=wrap(rendered,**journal), investigation_checkout=binding,
                  verification_workspace_for=action['request_id'])
    if directory:
        stored.update(receipt_directory=directory,command_digest=hashlib.sha256(rendered.encode()).hexdigest())
    fingerprint = digest({'verification_workspace_for':action['request_id'],'plan':stored})
    stamp = iso_now()
    conn.execute("INSERT INTO broker_remote_actions VALUES(?,?,?,?,?,'queued',?,?)",
                 (request_id,action['peer_uid'],action['grant_id'],fingerprint,canonical_json(stored),stamp,stamp))
    conn.execute('INSERT INTO project_verification_workspaces VALUES(?,?)',(action['request_id'],request_id))


def guard(conn, action):
    """Called on dispatch and every heartbeat; cannot authorize another test."""
    plan = json.loads(action['plan_json'])
    parent_id = plan.get('verification_workspace_for')
    if parent_id:
        row = conn.execute('SELECT * FROM project_verification_workspaces WHERE preparation_request_id=?',
                           (action['request_id'],)).fetchone()
        parent = conn.execute('SELECT * FROM broker_remote_actions WHERE request_id=?',(parent_id,)).fetchone()
        if (row is None or row['request_id'] != parent_id or parent is None or parent['state'] != 'queued'
                or parent['grant_id'] != action['grant_id'] or parent['peer_uid'] != action['peer_uid']):
            raise ValueError('verification workspace parent binding changed')
        from .project_verification_runs import validate_dispatch

        validate_dispatch(conn, parent)
        return 600
    row = conn.execute('SELECT a.*,r.exit_code,c.request_id AS observed FROM project_verification_workspaces w '
                       'JOIN broker_remote_actions a ON a.request_id=w.preparation_request_id '
                       'LEFT JOIN broker_remote_results r ON r.request_id=a.request_id '
                       'LEFT JOIN project_investigation_checkouts c ON c.request_id=a.request_id '
                       'WHERE w.request_id=?',(action['request_id'],)).fetchone()
    if row is not None:
        if (row['state'] != 'succeeded' or row['exit_code'] != 0 or row['observed'] is None
                or row['grant_id'] != action['grant_id'] or row['peer_uid'] != action['peer_uid']
                or json.loads(row['plan_json']).get('verification_workspace_for') != action['request_id']):
            raise ValueError('verification workspace preparation not confirmed')
    elif plan.get('verification_run_id') and plan.get('investigation_checkout'):
        raise ValueError('verification workspace binding unavailable')


def projection(conn, request_id):
    row = conn.execute('SELECT a.request_id,a.state,r.exit_code,c.request_id AS observed FROM project_verification_workspaces w '
                       'JOIN broker_remote_actions a ON a.request_id=w.preparation_request_id '
                       'LEFT JOIN broker_remote_results r ON r.request_id=a.request_id '
                       'LEFT JOIN project_investigation_checkouts c ON c.request_id=a.request_id WHERE w.request_id=?',
                       (request_id,)).fetchone()
    if row is None:
        return None
    result = {key:row[key] for key in ('request_id','state','exit_code')}
    code = row['exit_code']
    if (row['state'] == 'succeeded' and (code != 0 or row['observed'] is None)
            or row['state'] == 'failed' and (code is None or not 1 <= code <= 254 or code in (124,125))):
        result['state'] = 'unknown'
    return result
