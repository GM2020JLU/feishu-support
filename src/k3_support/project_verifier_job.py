"""A fresh coding grant for one operator-selected verification step/command."""

import json
from uuid import NAMESPACE_URL, uuid5

from . import coding_tasks
from .ids import canonical_json, digest
from .project_bugs import BugConflict, _bug, _text
from .project_investigation import CONTEXT
from .project_investigation import FIELDS as INVESTIGATION_FIELDS
from .project_verifier_repository_set import source_map, workspace_paths

FIELDS = INVESTIGATION_FIELDS | {'verification'}


def selection(value):
    if not isinstance(value,dict) or set(value) not in (
            {'plan_id','step_id','command'}, {'plan_id','step_id','command','sources'}):
        raise ValueError('verification job requires exact plan, step and command')
    for name in ('plan_id','step_id','command'):
        _text(value[name],name,16000 if name=='command' else 256)
    if '\0' in value['command'] or any(0xD800 <= ord(c) <= 0xDFFF for c in value['command']):
        raise ValueError('invalid verification command')
    if 'sources' in value and not isinstance(value['sources'], dict):
        raise ValueError('verification sources must be a repository map')
    return value


def plan_binding(conn, value, *, round_id, source, repository=None):
    selection(value)
    plan=conn.execute('SELECT p.*,r.archived_at FROM project_verification_plans p JOIN project_bug_rounds r USING(round_id) WHERE plan_id=?',
                      (value['plan_id'],)).fetchone()
    if (plan is None or plan['round_id'] != round_id or plan['archived_at'] is not None
            or plan['version'] != conn.execute('SELECT max(version) FROM project_verification_plans WHERE round_id=?',(round_id,)).fetchone()[0]):
        raise BugConflict('verification job plan is no longer current')
    definition=json.loads(plan['plan_json'])
    step=next((s for s in definition['steps'] if s['id']==value['step_id']),None)
    if (step is None or step['layer'] not in {'static','build','software_test','device_function'}
            or not step['repositories']):
        raise ValueError('verification job requires a software repository step')
    if step['layer'] != 'device_function' and (step['artifacts'] or step['devices']):
        raise ValueError('verification job currently requires software repositories without device/artifact operations')
    by_id={r['id']:r for r in definition['repositories']}
    repos=[by_id[key] for key in step['repositories']]
    names=[r['repository'] for r in repos]
    if len(names) != len(set(names)):
        raise ValueError('verification step requires distinct configured repositories')
    if len(names) > 20:
        raise ValueError('joint verification supports at most 20 repositories')
    if step['layer'] == 'device_function' and len(names) != 1:
        raise ValueError('joint device verification is not supported')
    primary= repos[0]
    if repository is not None and repository != primary['repository']:
        raise ValueError('verification primary repository differs from the investigation')
    selected=source_map(value,source,primary['repository'])
    if set(selected) != set(names):
        raise ValueError('verification source map must cover exactly the step repositories')
    execution_nodes={step['node'],*(r['node'] for r in repos)}
    if len(execution_nodes) != 1:
        raise ValueError('verification repositories must share the execution node')
    for repo in repos:
        candidate=selected[repo['repository']]
        if (candidate['branch'] != repo['branch'] or candidate['node'] != repo['node']
                or candidate['base_commit'] != repo['candidate_commit']):
            raise ValueError('verification source differs from the planned candidate')
    if step['depends_on']:
        from .project_verification_reviews import assessments

        states={s['step_id']:s['state'] for s in assessments(conn,plan['plan_id'])}
        if any(states.get(key) != 'passed' for key in step['depends_on']):
            raise BugConflict('verification job dependencies are not reviewed')
    return plan,step,primary


def submit(conn, config, payload):
    if not isinstance(payload,dict) or set(payload) != FIELDS:
        raise ValueError('verification job requires exact fields')
    bug=_bug(conn,payload['bug_id'])
    context={key:payload[key] for key in CONTEXT-{'actor'}}
    context.update(actor=config.control_operator_id,verification=selection(payload['verification']))
    request={key:payload[key] for key in coding_tasks.FIELDS-{'case_id'}}
    request['case_id']=bug['case_id']
    return coding_tasks.submit(conn,config,request,project_context=context)


def validate_job(conn, inputs):
    value=inputs.get('verification')
    if value is None:
        return None
    row=conn.execute('SELECT i.round_id,r.execution_state FROM project_investigation_jobs i JOIN project_bug_rounds r USING(round_id) WHERE i.job_id=?',(inputs['job_id'],)).fetchone()
    if row is None or row['execution_state'] not in {'planned','running'}:
        raise ValueError('verification job round binding unavailable or held')
    bound=plan_binding(conn,value,round_id=row['round_id'],source=inputs['investigation_source'],repository=inputs['repos'][0])
    selected=source_map(value,inputs['investigation_source'],inputs['repos'][0])
    if set(inputs['repos']) != set(selected):
        raise ValueError('verification input repositories differ from the planned source set')
    expected_order=[inputs['repos'][0]]+sorted(set(selected)-{inputs['repos'][0]})
    if inputs['repos'] != expected_order:
        raise ValueError('verification input repositories must keep the primary first')
    if len(selected)>1:
        if inputs.get('investigation_sources') != selected:
            raise ValueError('verification projected source map changed')
    elif 'investigation_sources' in inputs:
        raise ValueError('single-repository verifier has an unexpected source map')
    return bound


def identity(inputs, grant_id):
    return digest({'job_id':inputs['job_id'],'input_digest':inputs['input_digest'],'grant_id':grant_id,'verification':inputs['verification']})


def arm(conn, config, *, grant_id, inputs, case_id):
    if validate_job(conn,inputs) is None:
        return
    value=inputs['verification']
    _,step,repo=validate_job(conn,inputs)
    from .project_verification_runs import prepare

    intent_id=identity(inputs,grant_id)
    source_bindings=source_map(value,inputs['investigation_source'],repo['repository'])
    paths=workspace_paths(config,case_id,inputs['job_id'],[repo['repository']]+sorted(set(source_bindings)-{repo['repository']}))
    definition=json.loads(conn.execute('SELECT plan_json FROM project_verification_plans WHERE plan_id=?',(value['plan_id'],)).fetchone()[0])
    by_id={item['id']:item for item in definition['repositories']}
    source_paths={repo_id:paths[by_id[repo_id]['repository']] for repo_id in step['repositories']}
    prepare(conn,plan_id=value['plan_id'],step_id=value['step_id'],grant_id=grant_id,
            remote_request_id=str(uuid5(NAMESPACE_URL,'k3-verification:'+intent_id)),
            remote={'mode':'work','repo':repo['repository'],'command':value['command']},
            actor=config.control_operator_id,request_id='verifier-'+intent_id,config=config,
            source_paths=source_paths)


def require_remote(conn, inputs, request):
    value=inputs.get('verification')
    if value is None:
        return
    validate_job(conn,inputs)
    row=conn.execute('SELECT v.* FROM project_verification_runs v JOIN broker_grants g USING(grant_id) WHERE v.remote_request_id=? AND g.job_id=?',
                     (request['request_id'],inputs['job_id'])).fetchone()
    if (row is None or request['request_id'] != str(uuid5(NAMESPACE_URL,'k3-verification:'+identity(inputs,row['grant_id'])))
            or row['plan_id'] != value['plan_id'] or row['step_id'] != value['step_id']
            or row['remote_json'] != canonical_json(request['params']['remote'])
            or request['params']['remote'] != {'mode':'work','repo':inputs['repos'][0],'command':value['command']}):
        raise ValueError('verification job may submit only its prepared command')
