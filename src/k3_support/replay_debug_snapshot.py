"""Sealed broker-result review with finite verification fixtures, at most one model call."""
import copy
import hashlib
import os
import sqlite3
from pathlib import Path
from uuid import UUID

from .config import Config, validate_config
from .executors import ExecutionResult
from .ids import _replay_id_factory, digest
from .replay_debug import execute_next_debug, review_completed_debug
from .replay_history import _payload, _run_snapshot_data, _snapshot_data, _validate_timeout
from .replay_transcript import VerificationTranscript
from .timeutil import iso_now, observed_clock, parse_iso


def validate_execution(value):
    """A finite simulation, never executable code or claimed live evidence."""
    from .executors import ExecutorError, validate_codex_result_text

    if (not isinstance(value, dict) or set(value) != {'report', 'exit_status'}
            or not isinstance(value['report'], str)
            or not 1 <= len(value['report'].encode('utf-8')) <= 200000
            or (value['exit_status'] is not None and
                (type(value['exit_status']) is not int or not 0 <= value['exit_status'] <= 255))):
        raise ValueError('bounded report and simulated exit status required')
    try:
        validate_codex_result_text(value['report'])
    except ExecutorError as error:
        raise ValueError('invalid simulated Debug report') from error


def execute(request, path='/replay/snapshot.db'):
    if not isinstance(request, dict) or set(request) - {'execution'} != {'config', 'job_id', 'transcript', 'observed_at', 'answer'}:
        raise ValueError('invalid internal Debug replay envelope')
    if 'execution' in request:
        validate_execution(request['execution'])
    raw = copy.deepcopy(request['config'])
    raw['paths'] = {'database': '/tmp/replay.db', 'data_dir': '/tmp/replay'}
    config = Config(validate_config(raw), Path('/tmp/replay.yaml'))
    transcript = VerificationTranscript(request['transcript'])
    requests = []

    def capture(argv, prompt, timeout):
        if requests:
            raise ValueError('Debug replay model retry is disabled')
        requests.append({'argv': argv, 'prompt': prompt, 'timeout': timeout})
        if request['answer'] is None:
            raise ValueError('Debug replay model stage pending')
        return ExecutionResult(argv, 0, request['answer'], '')

    conn = sqlite3.connect(':memory:', isolation_level=None)
    token = None
    try:
        with open(path, 'rb') as source:
            data = source.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError('Debug snapshot exceeds limit')
        conn.deserialize(data)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        seed, counters = hashlib.sha256(data).hexdigest(), {}

        def identifier(prefix):
            counters[prefix] = counters.get(prefix, 0) + 1
            return f'{prefix}_{digest([seed, prefix, counters[prefix]])[:32]}'

        token = _replay_id_factory.set(identifier)
        with observed_clock(parse_iso(request['observed_at'])):
            if 'execution' not in request:
                result = review_completed_debug(conn, config, job_id=request['job_id'],
                    verification_runner=transcript, reviewer=capture)
            else:
                from .broker_execution_instances import register

                def report(inputs, heartbeat):
                    if inputs['job_id'] != request['job_id']:
                        raise ValueError('requested Debug job is not next eligible job')
                    return request['execution']['report']

                def observe(db, *, grant_id, claim_request_id):
                    status = request['execution']['exit_status']
                    if status is None:
                        return  # Report receipt alone is not exit evidence.
                    invocation = digest([seed, claim_request_id, 'simulated-invocation'])[:32]
                    register(db, grant_id=grant_id, claim_request_id=claim_request_id,
                             invocation_id=invocation,
                             cgroup_path=f'/replay/k3-support-broker-worker@{claim_request_id}.service')
                    db.execute('INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)',
                               (grant_id, invocation, os.geteuid() + 1, 1, status, request['observed_at']))

                result = execute_next_debug(conn, config, executor=report, observer=observe,
                    verification_runner=transcript, reviewer=capture, worker_uid=os.geteuid() + 1,
                    now=parse_iso(request['observed_at']),
                    claim_request_id=str(UUID(digest([seed, request['job_id'], 'replay-claim'])[:32])))
        return {'requests': requests, 'result': result, 'transcript': transcript.status()}
    finally:
        if token is not None:
            _replay_id_factory.reset(token)
        conn.close()


def run(database, request, *, reviewer, timeout=30):
    """Trusted reviewer receives the production prompt; owns network/deadline policy.

    No expected decision is accepted in public input. Transcript outputs are
    explicit simulations, not independently verified remote evidence.
    """
    _validate_timeout(timeout)
    if (not isinstance(request, dict) or set(request) - {'observed_at', 'execution'} != {'config', 'job_id', 'transcript'}
            or not callable(reviewer)):
        raise ValueError('Debug replay requires config, job, transcript and trusted reviewer')
    if not isinstance(request['job_id'], str) or not 1 <= len(request['job_id']) <= 256:
        raise ValueError('invalid Debug job ID')
    VerificationTranscript(request['transcript'])
    if 'execution' in request:
        validate_execution(request['execution'])
    at = request.get('observed_at', iso_now())
    parse_iso(at)
    envelope = copy.deepcopy({**request, 'observed_at': at, 'answer': None})
    _payload(envelope)
    data = _snapshot_data(database)
    initial = _run_snapshot_data(data, envelope, timeout=timeout, debug=True)
    observations = initial['requests']
    if not isinstance(observations, list) or len(observations) > 1:
        raise ValueError('invalid Debug model observations')
    calls = []
    final = initial
    if observations:
        fingerprint = digest(observations[0])
        answer = reviewer(copy.deepcopy(observations[0]))
        if not isinstance(answer, str) or not 1 <= len(answer) <= 200000:
            raise ValueError('Debug reviewer returned no bounded text; no retry')
        envelope['answer'] = answer
        _payload(envelope)
        final = _run_snapshot_data(data, envelope, timeout=timeout, debug=True)
        if final['requests'] != observations or final['transcript'] != initial['transcript']:
            raise ValueError('Debug replay observations changed after inference')
        calls.append({'stage': 'debug_review', 'input_digest': fingerprint})
    return {'result': final['result'], 'transcript': final['transcript'], 'calls': calls,
            'scope': ('sealed_simulated_debug_lifecycle_no_consumers' if 'execution' in request
                      else 'sealed_captured_debug_review_no_consumers'),
            'model_invoked': None, 'provider_verification': 'not_established_by_callback'}
