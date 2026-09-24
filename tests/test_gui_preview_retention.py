import threading
import time

import pytest
from test_gui_inference import payload, finished

from k3_support.gui_inference import PreviewTasks
from k3_support.ids import digest


def test_129_completed_previews_evict_body_not_deduplication(config):
    calls = []
    def runner(*args, **kwargs):
        calls.append(1)
        return {'private_result': 'synthetic'}
    tasks = PreviewTasks(config, runner)
    first = None
    for index in range(129):
        value = payload()
        if index == 0:
            first = value
        tasks.start('session', value)
        finished(tasks, 'session', value['request_id'])
    assert len(tasks.tasks) == 128
    assert len(tasks.receipts) == 129
    replay = tasks.submit('session', first)
    assert replay['state'] == 'completed' and replay['result_evicted']
    assert 'result' not in replay and len(calls) == 129
    with pytest.raises(ValueError, match='同一请求标识'):
        tasks.submit('session', {**first, 'event': {'changed': True}})
    with pytest.raises(ValueError):
        tasks.status('another-session', first['request_id'])


def test_receipt_limit_refuses_new_call_but_never_forgets_old_id(config):
    tasks = PreviewTasks(config, lambda *a, **kw: pytest.fail('receipt limit must prevent model call'))
    first = None
    for _ in range(512):
        value = payload()
        first = first or value
        tasks.receipts[('session', value['request_id'])] = {
            'request_id': value['request_id'], 'digest': digest(value), 'state': 'completed',
            'execution_state': 'completed', 'result_evicted': True}
    result = tasks.submit('session', payload())
    assert result['state'] == 'rejected' and result['execution_state'] == 'not_started'
    assert '512' in result['error']
    assert tasks.submit('session', first)['result_evicted']
    assert len(tasks.receipts) == 512


def test_logout_revokes_access_but_does_not_cancel_accepted_call(config):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def runner(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {'private_result': 'must be discarded'}
    tasks = PreviewTasks(config, runner, require_session_registration=True)
    tasks.activate_session('old')
    value = payload()
    tasks.start('old', value)
    assert entered.wait(2)
    try:
        tasks.revoke_session('old')
        with pytest.raises(ValueError, match='注销'):
            tasks.status('old', value['request_id'])
        tasks.activate_session('new')
        assert tasks.submit('new', payload())['execution_state'] == 'not_started'
        assert len(calls) == 1
    finally:
        release.set()
    deadline = time.monotonic()+3
    while time.monotonic() < deadline:
        with tasks.lock:
            if not tasks.tasks:
                break
        time.sleep(0.01)
    assert not tasks.tasks and not tasks.receipts
    assert tasks.submit('old', payload())['execution_state'] == 'not_started'


def test_thread_start_failure_is_distinguished_from_unknown_model_result(config, monkeypatch):
    tasks = PreviewTasks(config, lambda *a: pytest.fail('not started'))
    def fail(_):
        raise RuntimeError('thread unavailable')
    monkeypatch.setattr(threading.Thread, 'start', fail)
    value = payload()
    result = tasks.submit('session', value)
    assert result['execution_state'] == 'not_started'
    assert tasks.submit('session', value) == result


def test_expiry_is_checked_at_preview_admission_not_only_http_login(config):
    tasks = PreviewTasks(config, lambda *a, **k: pytest.fail('expired model call'), require_session_registration=True)
    tasks.activate_session('expired', expires_at=time.monotonic()-1)
    assert tasks.submit('expired', payload())['execution_state'] == 'not_started'
    assert not tasks.tasks and not tasks.receipts
