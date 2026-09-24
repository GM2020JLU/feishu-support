"""Bounded model transport around repeated execution of one sealed snapshot.

Routing, document selection and optional clarification review are supported. Never run a live
retrieval, delivery, coding or board consumer. Documents are supplied fixtures.
"""

import copy
import hashlib
import sqlite3
from pathlib import Path

from .config import Config, validate_config
from .ids import _replay_id_factory, digest
from .replay_assumptions import apply, clock, validate
from .replay_history import _payload, _run_snapshot_data, _snapshot_data, _validate_timeout
from .replay_research import replay_research_pipeline, validate_documents
from .timeutil import iso_now


def _events(request):
    if ('event' in request) == ('events' in request):
        raise ValueError('provide one event or a conversation, not both')
    events = request.get('events') if 'events' in request else [request['event']]
    if (not isinstance(events, list) or not 1 <= len(events) <= 10
            or any(not isinstance(event, dict) for event in events)):
        raise ValueError('conversation requires 1 to 10 event objects')
    return events


def execute(request, path='/replay/snapshot.db'):
    if (not isinstance(request, dict) or set(request) - {'event', 'events'} !=
            {'config', 'documents', 'assumptions', 'answers', 'review_clarification'}):
        raise ValueError('invalid internal pipeline envelope')
    events = _events(request)
    if type(request['review_clarification']) is not bool:
        raise ValueError('invalid clarification review flag')
    answers = request['answers']
    if not isinstance(answers, list) or len(answers) > len(events) * (3 if request['review_clarification'] else 2):
        raise ValueError('invalid pipeline answers')
    raw = copy.deepcopy(request['config'])
    raw['paths'] = {'database': '/tmp/replay.db', 'data_dir': '/tmp/replay'}
    config = Config(validate_config(raw), Path('/tmp/replay.yaml'))
    assumptions = validate(request['assumptions'])
    requests = []
    turn = 0

    # This is sandbox control flow, not a failed model result. Production
    # Exception handlers must not turn an unanswered phase into fallback work.
    class PendingInference(BaseException):
        pass

    def capture(stage, value):
        index = len(requests)
        requests.append({'stage': stage, 'turn': turn, 'input': copy.deepcopy(value), 'input_digest': digest(value)})
        if index >= len(answers):
            raise PendingInference
        return copy.deepcopy(answers[index]['value'])

    conn = sqlite3.connect(':memory:', isolation_level=None)
    id_token = None
    try:
        with open(path, 'rb') as source:
            data = source.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError('pipeline snapshot exceeds limit')
        conn.deserialize(data)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        # Generated references must be identical across phases of this sealed
        # snapshot. Context-local override never changes other threads or live
        # workers, and is reset even when production replay raises.
        seed = hashlib.sha256(data).hexdigest()
        counters = {}

        def replay_id(prefix):
            counters[prefix] = counters.get(prefix, 0) + 1
            return f'{prefix}_{digest([seed, prefix, counters[prefix]])[:32]}'

        id_token = _replay_id_factory.set(replay_id)
        results = []
        with clock(assumptions):
            for turn, event in enumerate(events):
                apply(conn, config, event, assumptions)
                try:
                    result = replay_research_pipeline(conn, config, event=event, documents=request['documents'],
                        router=lambda value: capture('routing', value),
                        selector=lambda value: capture('research_selection', value),
                        clarification_reviewer=(lambda value: capture('clarification_review', value))
                            if request['review_clarification'] else None)
                except PendingInference:
                    result = None
                    break
                results.append(result)
                # Never admit a later message until prior inference is applied.
                if len(requests) > len(answers):
                    break
        if 'events' in request:
            result = {'turns': results, 'scope': 'same_snapshot_conversation_no_consumers',
                      'model_quality_verified': False}
        return {'requests': requests, 'result': result}
    finally:
        if id_token is not None:
            _replay_id_factory.reset(id_token)
        conn.close()


def run(database, request, *, router, selector, clarification_reviewer=None, timeout=30):
    """At most three callback calls per turn; no retries or expected answers.

    The callbacks execute outside the networkless sandbox and own transport
    deadlines/authorization. Both source data and business clock are frozen.
    """
    _validate_timeout(timeout)
    if clarification_reviewer is not None and not callable(clarification_reviewer):
        raise ValueError('clarification reviewer must be a trusted callback')
    if (not isinstance(request, dict) or set(request) - {'assumptions', 'event', 'events'} != {'config', 'documents'}
            or not callable(router) or not callable(selector)):
        raise ValueError('pipeline requires config, event, documents and trusted callbacks')
    events = _events(request)
    assumptions = validate(request.get('assumptions', {}))
    validate_documents(request['documents'])
    assumptions.setdefault('observed_at', iso_now())
    callbacks = {'routing': router, 'research_selection': selector}
    if clarification_reviewer is not None:
        callbacks['clarification_review'] = clarification_reviewer
    envelope = copy.deepcopy({**request, 'assumptions': assumptions, 'answers': [],
                              'review_clarification': clarification_reviewer is not None})
    _payload(envelope)
    data = _snapshot_data(database)
    calls = []
    capacity = len(callbacks) * len(events)
    for _ in range(capacity + 1):
        phase = _run_snapshot_data(data, envelope, timeout=timeout, pipeline=True)
        inputs = phase['requests']
        if not isinstance(inputs, list) or len(inputs) > capacity or len(inputs) < len(calls):
            raise ValueError('pipeline stage sequence changed')
        for index, previous in enumerate(calls):
            if (inputs[index]['stage'] != previous['stage']
                    or inputs[index].get('turn', 0) != previous['turn']
                    or digest(inputs[index]['input']) != previous['input_digest']):
                raise ValueError('pipeline observations changed after inference')
        if len(inputs) == len(calls):
            return {'result': phase['result'], 'calls': calls, 'assumptions': assumptions,
                    'scope': ('sealed_snapshot_conversation_no_consumers' if 'events' in request
                              else 'sealed_snapshot_routing_and_fixture_research_no_consumers'),
                    'provider_verification': 'not_established_by_callback', 'model_invoked': None}
        item = inputs[len(calls)]
        stage = item['stage']
        turn = item.get('turn', 0)
        if (type(turn) is not int or not 0 <= turn < len(events)
                or stage not in callbacks or any(call['stage'] == stage and call['turn'] == turn for call in calls)):
            raise ValueError('unsupported or repeated pipeline stage')
        observation = {'stage': stage, 'turn': turn, 'input_digest': digest(item['input'])}
        value = callbacks[stage](copy.deepcopy(item['input']))
        if value is None:
            raise ValueError('model stage returned no result; pipeline stopped without retry')
        # Serialize before a later process is started; reject oversized/non-JSON
        # model outputs using the same bounded transfer envelope.
        envelope['answers'].append({'value': value})
        _payload(envelope)
        calls.append(observation)
    raise ValueError('pipeline exceeded configured inference stages')
