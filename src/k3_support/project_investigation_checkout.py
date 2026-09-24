"""Fixed checkout prelude and its control-generated first-frame observation."""

import json
import hashlib
import re
import shlex
from importlib.resources import files
from pathlib import PurePosixPath

from .ids import canonical_json
from .project_read_client import ProjectReadError, parse_json
from .timeutil import iso_now

PREFIX = 'K3_CHECKOUT_V1:'


def companions(conn, config, inputs, case_id):
    sources = inputs.get('investigation_sources')
    if sources is None:
        return None
    if set(sources) != set(inputs['repos']) or sources[inputs['repos'][0]] != inputs['investigation_source']:
        raise ValueError('joint checkout source set changed')
    from .project_investigation_candidate import resolve
    from .project_investigation_source import require_current

    result = []
    for name in inputs['repos'][1:]:
        source = sources[name]
        require_current(config, name, source)
        seed = resolve(conn, config, case_id=case_id, repository=name, source=source)
        item = {'repository': name, 'source_path': seed['path'] if seed else config.raw['repositories'][name]['path'],
                'base_commit': source['base_commit'],
                'relative_path': 'repositories/' + hashlib.sha256(name.encode()).hexdigest()[:16]}
        if seed:
            item['seed_job_id'] = seed['job_id']
        result.append(item)
    return result


def companion_seeds(binding):
    return [item['seed_job_id'] for item in binding.get('companions', []) if 'seed_job_id' in item]


def prepare(conn, config, inputs, *, job_id, case_id, request_id, remote):
    if remote['mode'] != 'work' or remote['repo'] != inputs['repos'][0]:
        raise ValueError('Bug investigation commands require the bound work repository')
    source = inputs['investigation_source']
    from .project_investigation_candidate import resolve

    seed = resolve(conn, config, case_id=case_id, repository=inputs['repos'][0], source=source)
    binding = {
        'job_id':job_id,'request_id':request_id,'repository':inputs['repos'][0],
        'source_path':seed['path'] if seed else config.raw['repositories'][inputs['repos'][0]]['path'],
        'base_commit':source['base_commit'],
        'root':str(PurePosixPath(config.runtime('remote_worktree_root')) / case_id / ('investigation-'+job_id)),
        'continuation':conn.execute('SELECT 1 FROM project_investigation_checkouts WHERE job_id=? LIMIT 1',(job_id,)).fetchone() is not None,
    }
    if seed:
        binding['seed_job_id'] = seed['job_id']
    extra = companions(conn, config, inputs, case_id)
    if extra is not None:
        binding['companions'] = extra
    sampler = files('k3_support').joinpath('verification_source_probe.py').read_text().rsplit('\nif __name__',1)[0]
    prelude = files('k3_support').joinpath('investigation_checkout_payload.py').read_text()
    command = shlex.join(['/usr/bin/python3','-I','-S','-c',sampler+'\n'+prelude,
                         canonical_json({'binding':binding,'command':remote['command']})])
    return command, binding


def record(conn, plan, result):
    """Called under the command-result transaction; models cannot call this."""
    expected = plan.get('investigation_checkout')
    if expected is None:
        return result
    if not isinstance(result,dict) or not isinstance(result.get('stdout'),str):
        raise TypeError('checkout observation unavailable')
    line, separator, rest = result['stdout'].partition('\n')
    if not separator or not line.startswith(PREFIX) or len(line.encode())>(32000 if 'companions' in expected else 16000):
        # Exit 126 is emitted by the fixed prelude without starting the command.
        if result.get('exit_code') == 126:
            return result
        raise ValueError('checkout observation unavailable')
    try:
        value = parse_json(line[len(PREFIX):])
    except ProjectReadError:
        raise ValueError('checkout observation unavailable') from None
    fields = {'request_id','job_id','base_commit','created','source'} | ({'companions'} if 'companions' in expected else set())
    if (not isinstance(value,dict) or set(value) != fields
            or any(value[k]!=expected[k] for k in ('request_id','job_id','base_commit'))
            or type(value['created']) is not bool):
        raise ValueError('checkout receipt binding changed')
    actual = value['source']
    if (not isinstance(actual,dict) or set(actual) != {'repository','path','head','end_head','base_is_ancestor','tracked_content_matches','tracked_count','matched','coverage'}
            or actual['repository'] != expected['repository']
            or actual['path'] != expected['root']+'/repository'
            or actual['coverage'] != 'tracked_source_sample'
            or any(type(actual[k]) is not bool for k in ('base_is_ancestor','tracked_content_matches','matched'))
            or type(actual['tracked_count']) is not int or not 0 <= actual['tracked_count'] <= 100000
            or any(not isinstance(actual[k],str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',actual[k]) for k in ('head','end_head'))
            or actual['head'] != actual['end_head'] or not actual['base_is_ancestor']
            or actual['matched'] != actual['tracked_content_matches']):
        raise ValueError('checkout source receipt inconsistent')
    if not expected['continuation'] and (actual['head'] != expected['base_commit'] or not actual['tracked_content_matches']):
        raise ValueError('initial checkout did not match clean baseline')
    if 'companions' in expected:
        observed = value['companions']
        if not isinstance(observed, list) or len(observed) != len(expected['companions']):
            raise ValueError('joint checkout observations incomplete')
        for binding, item in zip(expected['companions'], observed, strict=True):
            if (not isinstance(item, dict) or set(item) != set(actual)
                    or item['repository'] != binding['repository']
                    or item['path'] != expected['root'] + '/' + binding['relative_path']
                    or item['head'] != binding['base_commit'] or item['end_head'] != binding['base_commit']
                    or any(item[key] is not True for key in ('base_is_ancestor','tracked_content_matches','matched'))
                    or type(item['tracked_count']) is not int or not 0 <= item['tracked_count'] <= 100000
                    or item['coverage'] != 'tracked_source_sample'):
                raise ValueError('joint checkout candidate evidence changed')
    conn.execute('INSERT INTO project_investigation_checkouts VALUES(?,?,?,?)',
                 (expected['request_id'],expected['job_id'],canonical_json(value),iso_now()))
    return {**result,'stdout':rest}


def projection(conn,job_id):
    return [{'recorded_at':row['recorded_at'],**json.loads(row['observation_json'])}
            for row in conn.execute('SELECT * FROM project_investigation_checkouts WHERE job_id=? ORDER BY rowid DESC LIMIT 5',(job_id,))]


def validate_plan(config, inputs, plan, *, job_id, case_id, request_id, conn=None):
    from .project_investigation_candidate import resolve

    seed = resolve(conn, config, case_id=case_id, repository=inputs["repos"][0], source=inputs["investigation_source"])
    expected = plan.get('investigation_checkout')
    fields = {'job_id','request_id','repository','source_path','base_commit','root','continuation'}
    if seed:
        fields.add('seed_job_id')
    extra = companions(conn, config, inputs, case_id)
    if extra is not None:
        fields.add('companions')
    if not isinstance(expected,dict) or set(expected) != fields or type(expected['continuation']) is not bool:
        raise ValueError('checkout plan unavailable')
    identity = {'job_id':job_id,'request_id':request_id,'repository':inputs['repos'][0],
                'source_path':seed['path'] if seed else config.raw['repositories'][inputs['repos'][0]]['path'],
                'base_commit':inputs['investigation_source']['base_commit'],
                'root':str(PurePosixPath(config.runtime('remote_worktree_root')) / case_id / ('investigation-'+job_id))}
    if seed:
        identity['seed_job_id'] = seed['job_id']
    if extra is not None:
        identity['companions'] = extra
    if any(expected[key] != value for key,value in identity.items()):
        raise ValueError('checkout plan binding changed')
