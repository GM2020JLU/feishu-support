"""Replay persisted broker-result review in a disposable memory database.

This is a library adapter, not an OS sandbox. Both callbacks must be trusted
fixture/model adapters; no production executor is used by default.
"""
from .codex_result_source import read_result
from .review import handle_codex_completion
from .workflow_replay import ReplayBoundaryError, require_memory


def execute_next_debug(conn, config, *, executor, observer, verification_runner,
                       reviewer, worker_uid, now, claim_request_id):
    """Replay one queued Debug lifecycle using the production worker protocol.

    All callbacks are explicitly trusted replay adapters, not model-selected
    code. The observer must supply captured/synthetic instance and exit evidence
    to the memory DB; report acceptance alone never synthesizes an exit. No
    socket, process, board consumer or sender is started here. This library
    function is not an OS sandbox and must not expose callbacks to untrusted
    request data. Transport/executor failures propagate without retry.
    """
    require_memory(conn)
    if not all(callable(value) for value in (executor, observer, verification_runner, reviewer)):
        raise ValueError('explicit trusted Debug replay callbacks required')
    if now.tzinfo is None:
        raise ValueError('timezone required')
    from secrets import token_bytes

    from .broker_claim_receipts import claim
    from .broker_completion import reconcile
    from .broker_input import read
    from .broker_renew import renew
    from .broker_results import submit
    from .broker_start import authorize
    from .broker_worker import run_one

    control_key = token_bytes(32)  # Ephemeral replay authority; never returned.
    before = {row[0] for row in conn.execute('SELECT outbox_id FROM outbox')}

    def transport(request):
        method = request['method']
        if method == 'claim':
            result = claim(conn, config, request, peer_uid=worker_uid,
                           control_key=control_key, now=now)
        elif method == 'start':
            result = authorize(conn, config, request, peer_uid=worker_uid, now=now)
        elif method == 'renew':
            result = renew(conn, request, peer_uid=worker_uid, config=config, now=now)
        else:
            result = {'input': read, 'result': submit}[method](
                conn, request, peer_uid=worker_uid, now=now)
        return {'version': 1, 'request_id': request['request_id'], 'ok': True, 'result': result}

    worker = run_one(claim_request_id=claim_request_id, transport=transport, executor=executor)
    completion = review = None
    if worker['state'] == 'report_received':
        grant = conn.execute('SELECT g.grant_id FROM broker_grants g JOIN jobs j USING(job_id) '
                             'WHERE g.job_id=? AND g.attempt_no=j.attempt_no',
                             (worker['job_id'],)).fetchone()
        observer(conn, grant_id=grant['grant_id'], claim_request_id=claim_request_id)
        completion = reconcile(conn, grant_id=grant['grant_id'])
        if completion['state'] == 'review_pending':
            review = review_completed_debug(conn, config, job_id=worker['job_id'],
                verification_runner=verification_runner, reviewer=reviewer)
    return {'worker': worker, 'completion': completion, 'review': review,
            'outbox_intentions': [dict(row) for row in conn.execute('SELECT * FROM outbox')
                                 if row['outbox_id'] not in before],
            'scope': 'queued_debug_lifecycle_in_memory_no_consumers',
            'execution_evidence': 'caller_supplied_callbacks_not_live_evidence',
            'model_quality_verified': False, 'external_consumers': False}


def review_completed_debug(conn, config, *, job_id, verification_runner, reviewer):
    require_memory(conn)
    if not callable(verification_runner) or not callable(reviewer):
        raise ValueError('explicit trusted verification and review callbacks required')
    if not isinstance(job_id, str) or not job_id:
        raise ValueError('completed job ID required')
    job = conn.execute("SELECT * FROM jobs WHERE job_id=? AND job_type='codex'", (job_id,)).fetchone()
    if job is None or job['state'] != 'succeeded':
        raise ReplayBoundaryError('only an already completed Debug job can be reviewed')
    if not conn.execute('SELECT 1 FROM broker_grants WHERE job_id=?', (job_id,)).fetchone():
        raise ReplayBoundaryError('replay requires a captured broker result, not a host file')
    # Validate attempt, lifecycle, input and digest binding before callbacks.
    read_result(conn, job_id=job_id)
    before = {row[0] for row in conn.execute('SELECT outbox_id FROM outbox')}
    result = handle_codex_completion(conn, config, job_id=job_id,
                                    remote_runner=verification_runner, hermes_runner=reviewer)
    case = conn.execute('SELECT state,next_action FROM cases WHERE case_id=?', (job['case_id'],)).fetchone()
    return {'review': result, 'case': dict(case),
        'outbox_intentions': [dict(row) for row in conn.execute('SELECT * FROM outbox')
                             if row['outbox_id'] not in before],
        'scope': 'captured_broker_result_review_in_memory_no_consumers',
        'verification_scope': 'caller_supplied_callback_not_live_evidence',
        'model_quality_verified': False, 'external_consumers': False}
