import os
import sys

import pytest

from k3_support.coding_acp import ACPError, Connection

SERVER = r'''
import json, os, pathlib, sys, time
pathlib.Path('pid').write_text(str(os.getpid()))
def send(value):
    print(json.dumps({'jsonrpc': '2.0', **value}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        assert request['params']['clientCapabilities'] == {}
        send({'id': request['id'], 'result': {'protocolVersion': 1}})
    elif method == 'session/new':
        assert request['params']['cwd'] == os.getcwd()
        send({'id': request['id'], 'result': {'sessionId': 'fixture', 'configOptions': [
            {'id': 'model', 'type': 'select', 'currentValue': 'default',
             'options': [{'group': 'fixture', 'options': [{'value': 'bound-model'}, {'value': 'unconfirmed'}]}]}]}})
    elif method == 'session/set_config_option':
        value = request['params']['value']
        send({'id': request['id'], 'result': {'configOptions': [
            {'id': 'model', 'type': 'select', 'currentValue': value if value != 'unconfirmed' else 'default'}]}})
    elif method == 'session/prompt':
        text = request['params']['prompt'][0]['text']
        if text.startswith('private task'):
            assert text.encode() not in pathlib.Path('/proc/self/cmdline').read_bytes()
        if text == 'wait':
            time.sleep(30)
        elif text == 'wrong-session':
            send({'method': 'session/update', 'params': {'sessionId': 'other', 'update': {}}})
        elif text == 'error':
            send({'id': request['id'], 'error': {'code': -1, 'message': 'PRIVATE PAYLOAD'}})
        elif text in ('permission', 'permission-end-turn'):
            send({'id': 'permission-1', 'method': 'session/request_permission',
                  'params': {'sessionId': 'fixture', 'options': [{'optionId': 'allow', 'kind': 'allow_always'}]}})
            answer = json.loads(sys.stdin.readline())
            assert answer['result']['outcome']['outcome'] == 'cancelled'
            send({'id': request['id'], 'result': {'stopReason': 'cancelled' if text == 'permission' else 'end_turn'}})
        elif text in ('failed-tool', 'failed-tool-update'):
            send({'method': 'session/update', 'params': {'sessionId': 'fixture',
                  'update': {'sessionUpdate': 'tool_call' if text == 'failed-tool' else 'tool_call_update',
                             'toolCallId': 'operation', 'status': 'failed'}}})
            send({'id': request['id'], 'result': {'stopReason': 'end_turn'}})
        elif text == 'filesystem':
            send({'id': 'fs-1', 'method': 'fs/read_text_file', 'params': {'path': '/etc/passwd'}})
            answer = json.loads(sys.stdin.readline())
            assert answer['error']['code'] == -32601
            send({'id': request['id'], 'result': {'stopReason': 'end_turn'}})
        else:
            send({'method': 'session/update', 'params': {'sessionId': 'fixture',
                  'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}}})
            send({'id': request['id'], 'result': {'stopReason': 'end_turn'}})
'''


def connection(tmp_path, *, code=SERVER, **kwargs):
    return Connection(argv=[sys.executable, '-c', code], cwd=str(tmp_path),
                      env={'HOME': str(tmp_path)}, heartbeat=kwargs.pop('heartbeat', lambda: None),
                      timeout=kwargs.pop('timeout', 2), heartbeat_interval=.02, **kwargs)


def assert_reaped(tmp_path):
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / 'pid').read_text()), 0)


def test_real_session_updates_and_private_stdin(tmp_path):
    updates = []
    with connection(tmp_path, on_update=updates.append) as client:
        client.initialize()
        client.new_session()
        assert client.prompt('private task 中文')['stopReason'] == 'end_turn'
        assert updates[0]['content']['text'] == 'private task 中文'
    assert_reaped(tmp_path)


@pytest.mark.parametrize('announced,accepted', [('fixture', True), ('other', False)])
def test_update_before_session_new_result_is_bound_to_returned_session(tmp_path, announced, accepted):
    update = {'method': 'session/update', 'params': {'sessionId': announced,
              'update': {'sessionUpdate': 'available_commands_update'}}}
    code = SERVER.replace(
        "        assert request['params']['cwd'] == os.getcwd()\n",
        "        assert request['params']['cwd'] == os.getcwd()\n"
        "        send(" + repr(update) + ")\n",
        1,
    )
    updates = []
    with connection(tmp_path, code=code, on_update=updates.append) as client:
        client.initialize()
        if accepted:
            client.new_session()
            assert updates == [{'sessionUpdate': 'available_commands_update'}]
        else:
            with pytest.raises(ACPError, match='different session'):
                client.new_session()
            assert updates == []
    assert_reaped(tmp_path)


@pytest.mark.parametrize('prompt,match', [
    ('permission', 'did not complete'), ('wrong-session', 'different session'),
    ('error', 'rejected request'),
])
def test_denied_or_uncertain_turn_is_not_success(tmp_path, prompt, match):
    with pytest.raises(ACPError, match=match) as raised, connection(tmp_path) as client:
        client.initialize()
        client.new_session()
        client.prompt(prompt)
    assert 'PRIVATE PAYLOAD' not in str(raised.value)
    assert_reaped(tmp_path)


def test_client_never_exposes_filesystem(tmp_path):
    with connection(tmp_path) as client:
        client.initialize()
        client.new_session()
        client.prompt('filesystem')
    assert_reaped(tmp_path)


def test_revocation_reaps_agent(tmp_path):
    def heartbeat():
        if (tmp_path / 'pid').exists():
            raise ValueError('revoked')
    with pytest.raises(ValueError, match='revoked'), connection(tmp_path, heartbeat=heartbeat) as client:
        client.initialize()
        client.new_session()
        client.prompt('wait')
    assert_reaped(tmp_path)


def test_timeout_reaps_agent(tmp_path):
    with pytest.raises(TimeoutError), connection(tmp_path, timeout=.1) as client:
        client.initialize()
        client.new_session()
        client.prompt('wait')
    assert_reaped(tmp_path)


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_unterminated_flood_is_bounded(tmp_path, stream):
    code = f'import sys,time; sys.{stream}.write("x" * 1000000); sys.{stream}.flush(); time.sleep(30)'
    with pytest.raises(ACPError, match='limit'), connection(tmp_path, code=code,
                                                         output_limit=1024, frame_limit=512) as client:
        client.initialize()


@pytest.mark.parametrize('frame', [
    '{"jsonrpc":"2.0","id":1,"id":1,"result":{}}',
    '{"jsonrpc":"2.0","id":true,"result":{}}',
    '{"jsonrpc":"2.0","id":99,"result":{}}',
    '{"jsonrpc":"2.0","id":1,"result":{},"error":{}}',
    '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":true}}',
    '{"jsonrpc":"2.0","id":1,"result":{"x":NaN}}',
])
def test_malformed_or_uncorrelated_response_fails(tmp_path, frame):
    code = f'import sys,time; sys.stdin.readline(); print({frame!r}, flush=True); time.sleep(30)'
    with pytest.raises(ACPError), connection(tmp_path, code=code) as client:
        client.initialize()


def test_oversized_prompt_is_not_sent(tmp_path):
    with pytest.raises(ACPError, match='request limit'), connection(tmp_path, frame_limit=1024) as client:
        client.initialize()
        client.new_session()
        client.prompt('x' * 1024)
    assert_reaped(tmp_path)


def test_selects_exact_advertised_model(tmp_path):
    with connection(tmp_path) as client:
        client.initialize()
        client.new_session()
        client.select_option('model', 'bound-model')
        assert client.config_options[0]['currentValue'] == 'bound-model'


@pytest.mark.parametrize('value,match', [('missing', 'not advertised'), ('unconfirmed', 'not confirmed')])
def test_never_silently_falls_back_to_default_model(tmp_path, value, match):
    with pytest.raises(ACPError, match=match), connection(tmp_path) as client:
        client.initialize()
        client.new_session()
        client.select_option('model', value)


def test_failed_request_cannot_be_retried_on_connection(tmp_path):
    with connection(tmp_path) as client:
        client.initialize()
        client.new_session()
        with pytest.raises(ACPError, match='rejected request'):
            client.prompt('error')
        with pytest.raises(ACPError, match='not ready'):
            client.prompt('private task retry')


@pytest.mark.parametrize('prompt', ['permission', 'permission-end-turn', 'failed-tool', 'failed-tool-update'])
def test_denial_or_tool_failure_invalidates_success_and_connection(tmp_path, prompt):
    with connection(tmp_path) as client:
        client.initialize()
        client.new_session()
        with pytest.raises(ACPError, match='did not complete'):
            client.prompt(prompt)
        with pytest.raises(ACPError, match='not ready'):
            client.prompt('retry after denied operation')
    assert_reaped(tmp_path)
