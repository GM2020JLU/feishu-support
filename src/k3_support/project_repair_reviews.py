"""Operator review of the current round's independently observed code changes.

Read-time derivation invalidates old decisions without rewriting historical
receipts. No repair verdict grants a verification pass or a remote operation.
"""

import base64
import json
from contextlib import nullcontext

from .db import transaction
from .ids import canonical_json, digest, new_id
from .project_bugs import BugConflict, _event, _text
from .timeutil import iso_now

FIELDS={'bug_id','round_id','request_id','evidence_digest','expected_review_id',
        'verdict','rationale','attested'}


def _segments(conn,config,round_,repository,root_base,context,command,change):
    """Review every increment when a same-round job continues a prior candidate."""
    from .project_investigation_after import validate_changeset
    from .project_investigation_candidate import resolve

    result=[];seen=set()
    while True:
        if command['request_id'] in seen or len(seen)>=100:raise ValueError('changeset chain limit')
        seen.add(command['request_id'])
        result.append({'request_id':command['request_id'],'base_commit':change['base_commit'],
                       'head_commit':change['head_commit'],'commits':change['commits'],
                       'patch_sha256':change['patch_sha256'],
                       'patch_text':base64.b64decode(change['patch_b64']).decode('utf-8','replace')})
        if change['base_commit']==root_base:return list(reversed(result))
        request=context['source'].get('candidate_request_id')
        parent=conn.execute('SELECT o.request_id,o.observation_json,b.payload_json FROM project_investigation_checkout_after o JOIN project_investigation_jobs i USING(job_id) JOIN broker_inputs b USING(job_id) WHERE o.request_id=? AND i.round_id=?',(request,round_['round_id'])).fetchone()
        if parent is None:raise ValueError('missing round baseline chain')
        payload=json.loads(parent['payload_json'])
        context=payload['context_extra']['project_investigation']
        observation=json.loads(parent['observation_json'])
        if payload['repos']!=[repository] or observation['source']['head']!=change['base_commit']:
            raise ValueError('changeset parent mismatch')
        source={**context['source'],'candidate_request_id':request,'base_commit':observation['source']['head']}
        resolve(conn,config,case_id=round_['case_id'],repository=repository,source=source)
        change=validate_changeset(observation['changeset'],context['source'],observation['source'])
        if change['state']!='observed':raise ValueError('missing parent changeset')
        # Earlier untracked files are not inherited by the independently clean
        # committed candidate checkout; final workspace exclusions remain gated.
        command=parent


def detail(conn, config, *, bug_id, round_id):
    with nullcontext() if conn.in_transaction else transaction(conn,immediate=False):
        return _detail(conn,config,bug_id,round_id)


def _detail(conn, config, bug_id, round_id):
    from .project_investigation_after import validate_changeset
    from .project_investigation_candidate import resolve
    from .project_round_readiness import inspect

    round_=conn.execute('SELECT r.*,b.case_id FROM project_bug_rounds r JOIN project_bugs b USING(bug_id) WHERE r.round_id=? AND r.bug_id=?',(round_id,bug_id)).fetchone()
    if round_ is None:raise ValueError('repair investigation unavailable')
    blockers=[]
    if round_['archived_at'] is not None:blockers.append('round_archived')
    settled=inspect(conn,round_id)
    if not settled['ready']:blockers.append('execution_unsettled')
    jobs=conn.execute('SELECT j.*,b.payload_json FROM project_investigation_jobs i JOIN jobs j USING(job_id) LEFT JOIN broker_inputs b USING(job_id) WHERE i.round_id=? ORDER BY i.rowid LIMIT 101',(round_id,)).fetchall()
    if len(jobs)>100:blockers.append('scope_limit')
    fingerprints=[]; latest={}; root_bases={}
    for job in jobs:
        try:
            payload=json.loads(job['payload_json'])
            context=payload['context_extra']['project_investigation']
            if (digest(payload)!=job['input_digest'] or context['round_id']!=round_id
                    or context['bug_id']!=bug_id or payload['case_id']!=round_['case_id']):raise ValueError('input binding')
            if 'verification' in context:continue
            if len(payload['repos'])!=1:raise ValueError('input binding')
            repository=payload['repos'][0]
            root_bases.setdefault(repository,context['source']['base_commit'])
            fingerprints.append({'job_id':job['job_id'],'attempt':job['attempt_no'],
                                 'input':job['input_digest'],'state':job['state'],'lifecycle':job['lifecycle_round']})
            latest[repository]=(job,context)
        except (ValueError,TypeError,KeyError):
            blockers.append('input_unavailable')
            fingerprints.append({'job_id':job['job_id'],'input':job['input_digest'],'invalid':True})
    plan=conn.execute('SELECT plan_id,plan_json FROM project_verification_plans WHERE round_id=? ORDER BY version DESC LIMIT 1',(round_id,)).fetchone()
    required=set(latest)
    plan_repos=[]
    if plan:
        plan_repos=json.loads(plan['plan_json'])['repositories']
        required.update(r['repository'] for r in plan_repos)
    if not required:blockers.append('no_repositories')
    repositories=[]; observations=[]
    for repository in sorted(required):
        item={'repository':repository,'blockers':[]}
        repositories.append(item)
        if repository not in latest:
            item['blockers'].append('repository_not_investigated');continue
        job,context=latest[repository]
        item['job_id']=job['job_id']
        command=conn.execute('SELECT a.request_id,o.observation_json FROM broker_remote_actions a JOIN broker_grants g USING(grant_id) LEFT JOIN project_investigation_checkout_after o USING(request_id) WHERE g.job_id=? ORDER BY a.rowid DESC LIMIT 1',(job['job_id'],)).fetchone()
        observations.append({'repository':repository,'request_id':command['request_id'] if command else None,
                             'observation_digest':digest(command['observation_json']) if command else None})
        try:
            if config is None:raise ValueError('configuration unavailable')
            observation=json.loads(command['observation_json']) if command else {}
            source={**context['source'],'candidate_request_id':command['request_id'],
                    'base_commit':observation['source']['head']}
            resolve(conn,config,case_id=round_['case_id'],repository=repository,source=source)
            change=validate_changeset(observation['changeset'],context['source'],observation['source'])
            if change['state']!='observed':raise ValueError('changeset unavailable')
            item.update(request_id=command['request_id'],base_commit=change['base_commit'],
                        head_commit=change['head_commit'],commits=change['commits'],patch_sha256=change['patch_sha256'],
                        patch_text=base64.b64decode(change['patch_b64']).decode('utf-8','replace'))
            try:
                item['segments']=_segments(conn,config,round_,repository,root_bases[repository],context,command,change)
            except (ValueError,TypeError,KeyError):
                item['blockers'].append('round_changeset_chain_incomplete')
            item['review_base_commit']=root_bases[repository]
            if not change['tracked_content_matches']:item['blockers'].append('uncommitted_tracked_content')
            if base64.b64decode(change['untracked_paths_b64']):item['blockers'].append('untracked_content')
            if any(r['repository']==repository and r['candidate_commit']!=change['head_commit'] for r in plan_repos):
                item['blockers'].append('verification_candidate_mismatch')
        except (ValueError,TypeError,KeyError):
            item['blockers'].append('current_changeset_unavailable')
    if any(r['blockers'] for r in repositories):blockers.append('repository_evidence_incomplete')
    if (repositories and all('segments' in r for r in repositories)
            and not any(segment['commits'] for r in repositories for segment in r['segments'])):
        blockers.append('no_committed_change')
    evidence={'round':round_id,'archived':round_['archived_at'],'jobs':fingerprints,
              'observations':observations,'plan':dict(plan) if plan else None,
              'blockers':blockers,'repositories':[
                  {k:([{sk:sv for sk,sv in segment.items() if sk!='patch_text'} for segment in v]
                       if k=='segments' else v) for k,v in r.items() if k!='patch_text'}
                  for r in repositories]}
    fingerprint=digest(evidence)
    row=conn.execute('SELECT * FROM project_repair_reviews WHERE round_id=? ORDER BY rowid DESC LIMIT 1',(round_id,)).fetchone()
    review=dict(row) if row else None
    current=review is not None and review['evidence_digest']==fingerprint
    state=review['verdict'] if current else ('in_progress' if fingerprints else 'not_started')
    return {'bug_id':bug_id,'round_id':round_id,'evidence_digest':fingerprint,'evidence':evidence,'review':review,
            'review_state':'current' if current else 'stale' if review else 'not_reviewed',
            'repair_state':state,'repositories':repositories,'ready_blockers':blockers,
            'review_available':round_['archived_at'] is None and settled['ready'] and 'scope_limit' not in blockers,
            'can_mark_ready':not blockers,'verification_passed':False,'closure_authorized':False}


def record(conn,config,*,actor,payload):
    if not isinstance(payload,dict) or set(payload)!=FIELDS:raise ValueError('repair review requires exact fields')
    _text(actor,'actor')
    for key in ('bug_id','round_id','request_id','evidence_digest','rationale'):
        _text(payload[key],key,4000 if key=='rationale' else 256)
    if (payload['attested'] is not True or payload['verdict'] not in ('ready','in_progress','not_applicable')
            or payload['expected_review_id'] is not None and not isinstance(payload['expected_review_id'],str)):
        raise ValueError('explicit repair attestation required')
    signature=digest(payload)
    with transaction(conn):
        old=conn.execute('SELECT * FROM project_repair_reviews WHERE actor=? AND request_id=?',(actor,payload['request_id'])).fetchone()
        if old:
            if old['request_digest']!=signature:raise BugConflict('repair request reused for different content')
            return dict(old)
        view=detail(conn,config,bug_id=payload['bug_id'],round_id=payload['round_id'])
        previous=view['review']['review_id'] if view['review'] else None
        if (not view['review_available'] or view['evidence_digest']!=payload['evidence_digest']
                or previous!=payload['expected_review_id']):raise BugConflict('repair evidence or review changed; refresh')
        if payload['verdict']=='ready' and not view['can_mark_ready']:
            raise BugConflict('repair ready requires complete current repository evidence')
        review_id=new_id('prr')
        conn.execute('INSERT INTO project_repair_reviews VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (review_id,payload['round_id'],actor,payload['request_id'],signature,payload['evidence_digest'],canonical_json(view['evidence']),payload['verdict'],payload['rationale'],iso_now()))
        conn.execute('UPDATE project_bugs SET revision=revision+1 WHERE bug_id=?',(payload['bug_id'],))
        _event(conn,payload['bug_id'],actor,'repair_reviewed',{'review_id':review_id,'verdict':payload['verdict']},payload['round_id'])
        return dict(conn.execute('SELECT * FROM project_repair_reviews WHERE review_id=?',(review_id,)).fetchone())
