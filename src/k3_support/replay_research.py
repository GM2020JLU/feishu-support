"""Explicit fixture-backed retrieval completion in a disposable replay DB."""
from __future__ import annotations

import copy

from .job_failure import fail_attempt
from .lark import CommandResult
from .orchestrator import complete_retrieval_route, continue_failed_retrieval
from .retrieval import run_retrieval_job
from .store import claim_jobs
from .workflow_replay import ReplayBoundaryError, require_memory


def fixture_reviewer(template):
    """Resolve explicit scenario indexes; never invent favorable review content."""
    def reviewer(request):
        value = copy.deepcopy(template)
        index = value.pop('source_message_index')
        refs = value.pop('research_ref_indexes')
        if type(index) is not int or not 0 <= index < len(request['messages']):
            raise ValueError('invalid source message index')
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 20
                or any(type(i) is not int or not 0 <= i < len(request['available_sources_and_checks']) for i in refs)):
            raise ValueError('invalid research indexes')
        value['source_quote']['event_pk'] = request['messages'][index]['event_pk']
        value['research_refs'] = [request['available_sources_and_checks'][i]['ref'] for i in refs]
        return value
    return reviewer


def validate_documents(documents):
    if not isinstance(documents, list) or len(documents) > 20:
        raise ValueError('at most 20 fixture documents allowed')
    urls = set()
    for doc in documents:
        if not isinstance(doc, dict) or set(doc) != {'title', 'url', 'content'}:
            raise ValueError('invalid fixture document fields')
        for key, limit in [('title', 300), ('url', 2000), ('content', 12000)]:
            if not isinstance(doc[key], str) or len(doc[key]) > limit:
                raise ValueError('invalid fixture document text')
        if not doc['url'].startswith('https://') or doc['url'] in urls:
            raise ValueError('fixture URLs must be unique HTTPS links')
        urls.add(doc['url'])


def finish_research(conn, config, *, case_id, documents, selection=None,
                    clarification_reviewer=None, fail_transport=False, selector=None):
    require_memory(conn)
    if selector is not None and (not callable(selector) or selection is not None):
        raise ValueError('use either a trusted selector callback or a fixture selection')
    if type(fail_transport) is not bool:
        raise ValueError('fail_transport must be explicit boolean')
    validate_documents(documents)
    # Avoid claiming an unrelated scenario's job. Multi-pending scheduling is
    # deliberately unsupported until exact-job claim support exists.
    pending = conn.execute("SELECT job_id,case_id FROM jobs WHERE job_type='retrieve' AND state='queued'").fetchall()
    if len(pending) != 1 or pending[0]['case_id'] != case_id:
        raise ReplayBoundaryError('research replay requires one queued retrieval for this Case')
    claimed = claim_jobs(conn, 'replay-retriever', job_types=('retrieve',))
    if len(claimed) != 1 or claimed[0]['job_id'] != pending[0]['job_id']:
        raise ReplayBoundaryError('research claim unavailable')

    def transport(argv):
        if fail_transport:
            raise TimeoutError('synthetic retrieval timeout')
        if argv[:2] == ['drive', '+search']:
            return CommandResult({'has_more': False, 'results': [
                {'title_highlighted': doc['title'], 'summary_highlighted': '',
                 'result_meta': {'doc_types': 'DOCX', 'token': f'replay_{i}', 'url': doc['url']}}
                for i, doc in enumerate(documents)]}, 'user', [])
        if argv[:2] == ['im', '+messages-search']:
            return CommandResult({'messages': [], 'has_more': False}, 'user', [])
        if argv[:2] == ['docs', '+fetch'] and '--doc' in argv:
            url = argv[argv.index('--doc') + 1]
            doc = next((doc for doc in documents if doc['url'] == url), None)
            if doc:
                return CommandResult({'document': {'document_id': 'synthetic-replay',
                    'revision_id': 1, 'content': doc['content']}}, 'user', [])
        raise ReplayBoundaryError('unrecognized fixture retrieval operation')

    try:
        retrieval = run_retrieval_job(conn, config, job_id=claimed[0]['job_id'], runner=transport)
    except TimeoutError:
        if not fail_transport:
            raise
        job_id = claimed[0]['job_id']
        completion = continue_failed_retrieval(conn, config, job_id=job_id, error_class='TimeoutError',
            expected_attempt_no=int(claimed[0]['attempt_no']))
        fail_attempt(conn, job_id=job_id, attempt_no=int(claimed[0]['attempt_no']), error_class='TimeoutError')
        return {'retrieval': {'job_id': job_id, 'state': 'failed', 'error_class': 'TimeoutError'},
                'completion': completion, 'source_scope': 'synthetic_transport_failure', 'model_invoked': False}
    completion = complete_retrieval_route(conn, config, case_id=case_id,
        retrieval_result=retrieval, selector=selector if selector is not None else lambda _: selection,
        clarification_reviewer=clarification_reviewer)
    child = None
    if completion and completion.get('job_id'):
        row = conn.execute('SELECT job_type,state FROM jobs WHERE job_id=?',
                           (completion['job_id'],)).fetchone()
        child = dict(row) if row else None
    return {'retrieval': retrieval, 'completion': completion,
            'child_job': child,
            'source_scope': 'synthetic_documents_not_live_feishu',
            'model_invoked': None if selector is not None or clarification_reviewer is not None else False}


def replay_research_pipeline(conn, config, *, event, documents, router, selector,
                             clarification_reviewer=None):
    """Continue production inbound and retrieval on one caller-owned memory DB.

    Documents are explicitly supplied fixtures, not live Feishu retrieval.
    Callbacks are trusted model transports owned by the caller, not sandboxed
    Python. No workers/delivery are run and callback success is not model proof.
    """
    from .workflow_replay import replay_inbound

    require_memory(conn)
    if not callable(router) or not callable(selector):
        raise ValueError('pipeline requires explicit routing and selection callbacks')
    tracked = {table: {row[0] for row in conn.execute(f'SELECT {key} FROM {table}')}
               for table, key in [('outbox', 'outbox_id'), ('jobs', 'job_id')]}
    inbound = replay_inbound(conn, config, event, message_router=router)
    case_id = inbound['result'].get('case_id')
    research = None
    if case_id and conn.execute(
            "SELECT 1 FROM jobs WHERE case_id=? AND job_type='retrieve' AND state='queued'",
            (case_id,)).fetchone():
        research = finish_research(conn, config, case_id=case_id, documents=documents,
                                   selector=selector, clarification_reviewer=clarification_reviewer)
    intentions = {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table}')
                          if row[key] not in tracked[table]]
                  for table, key in [('outbox', 'outbox_id'), ('jobs', 'job_id')]}
    return {'inbound': inbound, 'research': research, 'intentions': intentions,
            'scope': 'inbound_and_fixture_research_same_memory_snapshot_no_consumers',
            'model_invoked': None, 'model_quality_verified': False,
            'source_scope': 'supplied_documents_not_live_feishu'}
