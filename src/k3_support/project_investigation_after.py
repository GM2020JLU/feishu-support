"""Post-command source sampling independent of the worker's output/report."""

import base64
import hashlib
import json
import re
import shlex
from importlib.resources import files

from .execution_transport import plan_argv
from .ids import canonical_json
from .project_read_client import ProjectReadError, parse_json
from .timeutil import iso_now


def collect(plan, *, transport, heartbeat):
    binding = plan['investigation_checkout']
    expected = {'repository':binding['repository'],'path':binding['root']+'/repository',
                'base_commit':binding['base_commit'],'candidate_commit':binding['base_commit']}
    observation = {'state':'unavailable','source':None}
    try:
        script = files('k3_support').joinpath('verification_source_probe.py').read_text().rsplit('\nif __name__',1)[0]
        script += '\n' + files('k3_support').joinpath('investigation_changeset_probe.py').read_text()
        command = shlex.join(['/usr/bin/python3','-I','-S','-c',script,canonical_json([expected])])
        heartbeat()
        result = transport(argv=plan_argv(plan,command),cwd='/',stdin=b'',heartbeat=heartbeat,
                           env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},timeout=180,
                           heartbeat_interval=1,output_limit=2*1024*1024,detailed=True)
        if (not isinstance(result,dict) or type(result.get('exit_code')) is not int
                or result['exit_code'] != 0 or not isinstance(result.get('stdout'),str)
                or len(result['stdout'].encode())>2*1024*1024):
            raise ValueError('post-command sampler unavailable')
        value = parse_json(result['stdout'])
        if not isinstance(value,dict) or set(value) not in ({'sources'},{'sources','changeset'}) or not isinstance(value['sources'],list) or len(value['sources']) != 1:
            raise ValueError('post-command sampler schema changed')
        source = value['sources'][0]
        if (not isinstance(source,dict) or set(source) != {'repository','path','head','end_head','base_is_ancestor','tracked_content_matches','tracked_count','matched','coverage'}
                or any(source[key] != expected[key] for key in ('repository','path'))
                or source['coverage'] != 'tracked_source_sample'
                or any(type(source[key]) is not bool for key in ('base_is_ancestor','tracked_content_matches','matched'))
                or type(source['tracked_count']) is not int or not 0 <= source['tracked_count'] <= 100000
                or any(not isinstance(source[key],str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',source[key]) for key in ('head','end_head'))):
            raise ValueError('post-command source binding changed')
        matches_base = (source['head'] == source['end_head'] == expected['base_commit']
                        and source['base_is_ancestor'] and source['tracked_content_matches'])
        if source['matched'] != matches_base:
            raise ValueError('post-command source observation inconsistent')
        heartbeat()
        state = ('mismatch' if not source['base_is_ancestor'] or source['head'] != source['end_head']
                 else 'clean' if source['tracked_content_matches'] else 'dirty')
        observation = {'state':state,'source':source}
        if 'changeset' in value:
            observation['changeset'] = validate_changeset(value['changeset'], expected, source)
    except (OSError,ValueError,TypeError,KeyError,ProjectReadError):
        pass
    return observation


def validate_changeset(value, binding, source):
    if value == {'state':'unavailable'}:
        return value
    fields={'state','coverage','base_commit','head_commit','commits','paths_b64',
            'patch_b64','patch_sha256','untracked_paths_b64','tracked_content_matches'}
    if (not isinstance(value,dict) or set(value)!=fields or value['state']!='observed'
            or value['coverage']!='complete_committed_changeset_v1'
            or value['base_commit']!=binding['base_commit'] or value['head_commit']!=source['head']
            or type(value['tracked_content_matches']) is not bool
            or value['tracked_content_matches']!=source['tracked_content_matches']
            or not source['base_is_ancestor'] or source['head']!=source['end_head']
            or not isinstance(value['commits'],list)
            or any(not isinstance(c,str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',c) for c in value['commits'])
            or len(set(value['commits']))!=len(value['commits'])):
        raise ValueError('changeset binding mismatch')
    if ((not value['commits']) != (value['base_commit']==value['head_commit'])
            or value['commits'] and value['commits'][-1]!=value['head_commit']):
        raise ValueError('changeset commit range mismatch')
    decoded={}
    for key in ('paths_b64','patch_b64','untracked_paths_b64'):
        if not isinstance(value[key],str):
            raise ValueError('invalid changeset encoding')
        decoded[key]=base64.b64decode(value[key],validate=True)
        if len(decoded[key])>256*1024:
            raise ValueError('changeset too large')
    if hashlib.sha256(decoded['patch_b64']).hexdigest()!=value['patch_sha256']:
        raise ValueError('changeset patch digest mismatch')
    for key in ('paths_b64','untracked_paths_b64'):
        if decoded[key] and not decoded[key].endswith(b'\0'):
            raise ValueError('incomplete changeset paths')
    return value


def record(conn, plan, observation):
    binding = plan['investigation_checkout']
    conn.execute('INSERT INTO project_investigation_checkout_after VALUES(?,?,?,?,?)',
                 (binding['request_id'],binding['job_id'],observation['state'],
                  canonical_json(observation),iso_now()))


def projection(conn,job_id):
    return [{'request_id':row['request_id'],'recorded_at':row['recorded_at'],**json.loads(row['observation_json'])}
            for row in conn.execute('SELECT * FROM project_investigation_checkout_after WHERE job_id=? ORDER BY rowid DESC LIMIT 5',(job_id,))]


def for_request(conn,request_id):
    row = conn.execute('SELECT observation_json FROM project_investigation_checkout_after WHERE request_id=?',(request_id,)).fetchone()
    return json.loads(row['observation_json']) if row else None
