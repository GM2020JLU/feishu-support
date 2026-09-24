import json
import sys
from dataclasses import replace

import pytest

from k3_support.broker_claude import executor
from k3_support.broker_execution_contract import ExecutionContract


def setup(tmp_path, result=None, exit_code=0):
    path = tmp_path / 'fake-claude'
    response = result if result is not None else {'type': 'result', 'subtype': 'success',
                                                  'is_error': False, 'result': 'fixture report'}
    path.write_text(f'''#!{sys.executable}
import json,os,pathlib,sys
prompt=sys.stdin.read()
assert "private colleague input" in prompt
assert "private colleague input" not in pathlib.Path('/proc/self/cmdline').read_text()
assert "private-capability" not in pathlib.Path('/proc/self/cmdline').read_text()
assert os.environ['ANTHROPIC_BASE_URL']=='https://provider.example'
pathlib.Path('argv.json').write_text(json.dumps(sys.argv[1:]))
print({json.dumps(response)!r})
raise SystemExit({exit_code})
''')
    path.chmod(0o700)
    policy = ExecutionContract('fixture', 'https://provider.example', 'bound-model', 'high',
                               'messages', 'a'*64, agent='claude')
    env = {'K3_SUPPORT_BROKER_TASK': 'private-capability', 'K3_SUPPORT_BROKER_SOCKET': '/tmp/fixture.sock',
           'K3_SUPPORT_BROKER_CONTROL_UID': '1234'}
    callback = executor(executable=path, workdir=tmp_path, environment=env, contract=policy,
                        remote_python=sys.executable)
    inputs = {'brief': 'private colleague input', 'model': policy.model, 'reasoning': policy.reasoning,
              'execution': policy.selection()}
    return callback, inputs, policy


def test_real_child_uses_private_stdin_and_only_bound_mcp_permissions(tmp_path):
    callback, inputs, _ = setup(tmp_path)
    assert callback(inputs, lambda: None) == 'fixture report'
    argv = json.loads((tmp_path / 'argv.json').read_text())
    assert argv[argv.index('--tools')+1] == ''
    assert argv[argv.index('--setting-sources')+1] == ''
    assert argv[argv.index('--permission-mode')+1] == 'dontAsk'
    assert '--strict-mcp-config' in argv and '--restricted' in argv
    assert '--fallback-model' not in argv and '--dangerously-skip-permissions' not in argv
    assert argv[argv.index('--allowedTools')+1].split(',') == [
        'mcp__k3_remote__verification_list', 'mcp__k3_remote__remote_submit', 'mcp__k3_remote__remote_read']
    mcp = json.loads(argv[argv.index('--mcp-config')+1])['mcpServers']['k3_remote']
    assert mcp['env']['K3_SUPPORT_BROKER_TASK'] == '${K3_SUPPORT_BROKER_TASK}'


@pytest.mark.parametrize('change', [{'model': 'different'}, {'reasoning': 'low'},
                                    {'execution': None}, {'brief': ''}])
def test_input_mismatch_never_launches(tmp_path, change):
    callback, inputs, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        callback({**inputs, **change}, lambda: None)
    assert not (tmp_path / 'argv.json').exists()


@pytest.mark.parametrize('result,exit_code', [
    ({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'PRIVATE ERROR'}, 0),
    ({'type': 'result', 'subtype': 'error_max_turns', 'is_error': False, 'result': 'partial'}, 0),
    ({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': ''}, 0),
    ({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'partial'}, 2),
    ({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'partial',
      'permission_denials': [{'tool_name': 'mcp__k3_remote__board_read'}]}, 0),
])
def test_failed_or_partial_response_is_not_a_report(tmp_path, result, exit_code):
    callback, inputs, _ = setup(tmp_path, result, exit_code)
    with pytest.raises(ValueError) as error:
        callback(inputs, lambda: None)
    assert 'PRIVATE ERROR' not in str(error.value)


def test_wrong_adapter_contract_rejected(tmp_path):
    _, _, policy = setup(tmp_path)
    with pytest.raises(ValueError, match='Claude execution contract'):
        executor(executable='/synthetic/claude', workdir=tmp_path, environment={},
                 contract=replace(policy, agent='codex'), remote_python=sys.executable)


def test_private_api_key_reaches_native_process_without_command_line(tmp_path, monkeypatch):
    home = tmp_path / 'native-home'
    home.mkdir(mode=0o700)
    key = home / 'api-key'
    key.write_text('synthetic-worker-key\n')
    key.chmod(0o600)
    policy = ExecutionContract('fixture', 'https://provider.example', 'bound-model', 'high',
                               'messages', 'a' * 64, agent='claude')
    captured = []
    monkeypatch.setattr('k3_support.broker_claude.run_process', lambda **kw: captured.append(kw) or json.dumps(
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'report'}))
    env = {'CLAUDE_CONFIG_DIR': str(home), 'K3_SUPPORT_BROKER_TASK': '{}',
           'K3_SUPPORT_BROKER_SOCKET': '/tmp/synthetic.sock', 'K3_SUPPORT_BROKER_CONTROL_UID': '1234'}
    execute = executor(executable='/synthetic/claude', workdir=tmp_path, environment=env,
                       contract=policy, remote_python=sys.executable)
    assert execute({'brief': 'synthetic task', 'model': policy.model, 'reasoning': policy.reasoning,
                    'execution': policy.selection()}, lambda: None) == 'report'
    assert captured[0]['env']['ANTHROPIC_API_KEY'] == 'synthetic-worker-key'
    assert 'synthetic-worker-key' not in str(captured[0]['argv'])
    assert 'synthetic-worker-key' not in str(captured[0]['stdin'])
    assert 'ANTHROPIC_API_KEY' not in env


@pytest.mark.parametrize('problem', ['empty', 'newline', 'oversized', 'public', 'symlink', 'hardlink', 'fifo'])
def test_unsafe_api_key_is_rejected_without_fallback(tmp_path, problem):
    import os

    from k3_support.broker_claude import _api_key

    home = tmp_path / 'credentials'
    home.mkdir(mode=0o700)
    key = home / 'api-key'
    key.write_text('fixture-key')
    key.chmod(0o600)
    if problem == 'empty': key.write_text('')
    elif problem == 'newline': key.write_text('first\nsecond')
    elif problem == 'oversized': key.write_text('x' * 16385)
    elif problem == 'public': key.chmod(0o644)
    elif problem == 'hardlink': os.link(key, home / 'copy')
    elif problem == 'symlink':
        key.rename(home / 'target')
        key.symlink_to('target')
    else:
        key.unlink()
        os.mkfifo(key, 0o600)
    with pytest.raises((ValueError, OSError)):
        _api_key(home)


def test_missing_api_key_preserves_native_oauth_mode(tmp_path):
    from k3_support.broker_claude import _api_key

    tmp_path.chmod(0o700)
    assert _api_key(tmp_path) is None


@pytest.mark.parametrize('problem', ['public_directory', 'linked_directory', 'relative'])
def test_credential_directory_must_be_private_and_explicit(tmp_path, problem):
    from k3_support.broker_claude import _api_key

    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    if problem == 'public_directory':
        home.chmod(0o755)
    elif problem == 'linked_directory':
        link = tmp_path / 'link'
        link.symlink_to(home, target_is_directory=True)
        home = link
    else:
        home = 'relative-home'
    with pytest.raises((ValueError, OSError)):
        _api_key(home)
