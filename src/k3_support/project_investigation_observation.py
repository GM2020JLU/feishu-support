"""Independent source-repository observations; never worker-provided logs."""

import json
import re
import shlex
from importlib.resources import files

from .broker_input import project
from .db import transaction
from .execution_transport import plan_argv, target
from .ids import canonical_json, new_id
from .project_investigation_source import require_current
from .project_read_client import ProjectReadError, parse_json
from .timeutil import iso_now


def observe(conn, config, action, *, transport, heartbeat):
    row = conn.execute(
        'SELECT j.* FROM jobs j JOIN broker_grants g USING(job_id) WHERE g.grant_id=?',
        (action['grant_id'],),
    ).fetchone()
    with transaction(conn):
        inputs = project(conn,job_id=row['job_id'],case_id=row['case_id'],
                         lifecycle_round=row['lifecycle_round'],input_digest=row['input_digest'],
                         request_id=action['request_id'])
    if 'investigation_source' not in inputs:
        return None
    source = inputs['investigation_source']
    repository = inputs['repos'][0]
    state, value = 'unavailable', {'reason':'baseline_observation_unavailable'}
    try:
        require_current(config,repository,source)
        from .project_investigation_candidate import resolve

        seed = resolve(conn, config, case_id=row['case_id'], repository=repository, source=source)
        if seed:
            from .project_investigation_after import collect

            sampled = collect({**target(config), 'investigation_checkout': {
                'repository':repository, 'root':seed['path'].removesuffix('/repository'),
                'base_commit':source['base_commit']}}, transport=transport, heartbeat=heartbeat)
            resolve(conn, config, case_id=row['case_id'], repository=repository, source=source)
            value = sampled
            state = ('unavailable' if sampled['state'] == 'unavailable' else
                     'matched' if sampled['state'] == 'clean' and sampled['source']['matched'] else 'mismatch')
            with transaction(conn):
                conn.execute('INSERT INTO project_investigation_source_observations VALUES(?,?,?,?,?)',
                             (new_id('piso'),action['request_id'],state,canonical_json(value),iso_now()))
            return state
        expected = {'repository':repository,'path':config.raw['repositories'][repository]['path'],
                    'branch':source['branch'],'base_commit':source['base_commit']}
        script = files('k3_support').joinpath('investigation_baseline_probe.py').read_text()
        command = shlex.join(['/usr/bin/python3','-I','-S','-c',script,canonical_json(expected)])
        heartbeat()
        result = transport(argv=plan_argv(target(config),command),cwd='/',stdin=b'',
                           heartbeat=heartbeat,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
                           timeout=90,heartbeat_interval=1,output_limit=16000,detailed=True)
        if (not isinstance(result,dict) or type(result.get('exit_code')) is not int
                or result['exit_code'] != 0 or not isinstance(result.get('stdout'),str)
                or len(result['stdout'].encode()) > 16000):
            raise ValueError('baseline probe failed')
        actual = parse_json(result['stdout'])
        if (not isinstance(actual,dict) or set(actual) != {
                'repository','path','branch','base_commit','branch_commit','end_branch_commit',
                'base_is_ancestor','coverage'}
                or any(actual[key] != expected[key] for key in ('repository','path','branch'))
                or actual['coverage'] != 'source_repository_baseline'
                or type(actual['base_is_ancestor']) is not bool
                or any(not isinstance(actual[key],str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',actual[key])
                       for key in ('base_commit','branch_commit','end_branch_commit'))):
            raise ValueError('baseline probe binding changed')
        heartbeat()
        require_current(config,repository,source)
        value = actual
        state = 'matched' if (actual['base_commit'] == expected['base_commit']
                             and actual['branch_commit'] == actual['end_branch_commit']
                             and actual['base_is_ancestor']) else 'mismatch'
    except (OSError,ValueError,TypeError,KeyError,ProjectReadError):
        pass
    with transaction(conn):
        conn.execute('INSERT INTO project_investigation_source_observations VALUES(?,?,?,?,?)',
                     (new_id('piso'),action['request_id'],state,canonical_json(value),iso_now()))
    return state


def projection(conn, job_id):
    return [{'state':row['state'],'recorded_at':row['recorded_at'],
             'source':json.loads(row['observation_json'])}
            for row in conn.execute(
                'SELECT o.* FROM project_investigation_source_observations o '
                'JOIN broker_remote_actions a USING(request_id) JOIN broker_grants g USING(grant_id) '
                'WHERE g.job_id=? ORDER BY o.rowid DESC LIMIT 5',(job_id,))]
