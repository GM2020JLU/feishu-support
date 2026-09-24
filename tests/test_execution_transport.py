import json
import subprocess

import pytest
from conftest import config_data
from test_broker_remote_runner import remote  # noqa: F401

from k3_support.broker_remote_probe import observe
from k3_support.broker_remote_runner import run_one
from k3_support.config import ConfigError, validate_config
from k3_support.execution_transport import command_argv, matches, plan_argv, target
from k3_support.preflight import runtime_doctor


@pytest.mark.parametrize('mode', [None, '', 'automatic', True, {}, []])
def test_unknown_transport_never_defaults_to_local(tmp_path, mode):
    data = config_data(tmp_path)
    data.setdefault('runtime', {})['remote_transport'] = mode
    with pytest.raises(ConfigError, match='remote_transport'):
        validate_config(data)


def test_local_requires_explicit_localhost_and_preserves_ssh_default(tmp_path):
    data = config_data(tmp_path)
    assert validate_config(data)['runtime']['remote_transport'] == 'ssh'
    data.setdefault('runtime', {})['remote_transport'] = 'local'
    with pytest.raises(ConfigError, match='localhost'):
        validate_config(data)
    data['runtime']['remote_host'] = 'localhost'
    assert validate_config(data)['runtime']['remote_transport'] == 'local'


def test_historical_plan_cannot_be_reinterpreted_as_local(config):
    config.raw['runtime']['remote_host'] = 'localhost'
    previous = target(config)
    previous.pop('transport')
    assert matches(config, previous)
    config.raw['runtime']['remote_transport'] = 'local'
    assert not matches(config, previous)
    command = 'printf "%s" "fixture only"'
    assert command_argv(config, command) == ['/bin/bash', '--noprofile', '--norc', '-c', command]
    with pytest.raises(ValueError, match='target'):
        plan_argv({'transport': 'local', 'host': 'other'}, command)
    with pytest.raises(ValueError, match='unknown'):
        plan_argv({'transport': 'automatic'}, command)


@pytest.fixture
def local_remote(config, request):
    config.raw['runtime'].update(remote_transport='local', remote_host='localhost',
                                 remote_receipt_directory='/var/lib/fixture-receipts')
    return request.getfixturevalue('remote')


def test_local_broker_preserves_guard_heartbeat_and_result_binding(conn, local_remote):
    cfg, reader = local_remote
    plan = json.loads(conn.execute('SELECT plan_json FROM broker_remote_actions').fetchone()[0])
    assert plan['transport'] == 'local' and plan['guard_version'] == 2
    def execute(**kw):
        assert kw['argv'] == ['/bin/bash', '--noprofile', '--norc', '-c', plan['command']]
        assert kw['keepalive'] is True and kw['heartbeat_interval'] == 1
        kw['heartbeat']()
        return {'exit_code': 0, 'stdout': 'local fixture output', 'stderr': ''}
    assert run_one(conn, cfg, contract_reader=reader, transport=execute)['state'] == 'succeeded'
    assert conn.execute('SELECT stdout FROM broker_remote_results').fetchone()[0] == 'local fixture output'
    assert run_one(conn, cfg, contract_reader=reader, transport=lambda **_: pytest.fail('rerun'))['state'] == 'idle'


@pytest.mark.parametrize('change', ['transport', 'host', 'ssh_command'])
def test_changed_execution_target_cancels_queued_work_and_refuses_receipt(conn, local_remote, change):
    cfg, reader = local_remote
    request_id = conn.execute('SELECT request_id FROM broker_remote_actions').fetchone()[0]
    cfg.raw['runtime'][{'transport': 'remote_transport', 'host': 'remote_host', 'ssh_command': 'ssh_command'}[change]] = {
        'transport': 'ssh', 'host': 'other-node', 'ssh_command': '/other/ssh'}[change]
    assert run_one(conn, cfg, contract_reader=reader,
                   transport=lambda **_: pytest.fail('changed target executed'))['state'] == 'cancelled'
    with pytest.raises(ValueError):
        observe(conn, cfg, request_id=request_id, transport=lambda **_: pytest.fail('changed target probed'))


def test_local_receipt_probe_is_read_only_and_uses_same_target(conn, local_remote):
    cfg, _ = local_remote
    row = conn.execute('SELECT * FROM broker_remote_actions').fetchone()
    plan = json.loads(row['plan_json'])
    before = list(conn.iterdump())
    def execute(**kw):
        assert kw['argv'][:4] == ['/bin/bash', '--noprofile', '--norc', '-c']
        assert 'read_receipt' in kw['argv'][4]
        value = {'state': 'guardian_returned', 'version': 1, 'request_id': row['request_id'],
                 'command_digest': plan['command_digest'], 'guard_exit_code': 0}
        return {'exit_code': 0, 'stdout': json.dumps(value, sort_keys=True) + '\n', 'stderr': ''}
    result = observe(conn, cfg, request_id=row['request_id'], transport=execute)
    assert result['state'] == 'guardian_returned' and not result['recovery_authorized']
    assert list(conn.iterdump()) == before


def test_historical_ssh_action_keeps_its_original_transport(conn, request):
    cfg, reader = request.getfixturevalue("remote")
    plan = json.loads(conn.execute('SELECT plan_json FROM broker_remote_actions').fetchone()[0])
    plan.pop('transport')
    conn.execute('UPDATE broker_remote_actions SET plan_json=?', (json.dumps(plan),))
    def execute(**kw):
        assert kw['argv'][:6] == [cfg.runtime('ssh_command'), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', cfg.runtime('remote_host')]
        return {'exit_code': 0, 'stdout': '', 'stderr': ''}
    assert run_one(conn, cfg, contract_reader=reader, transport=execute)['state'] == 'succeeded'


def test_target_change_during_local_execution_remains_unknown(conn, local_remote):
    cfg, reader = local_remote
    def execute(**kw):
        cfg.raw['runtime']['remote_transport'] = 'ssh'
        kw['heartbeat']()
        pytest.fail('changed target heartbeat accepted')
    with pytest.raises(ValueError, match='changed'):
        run_one(conn, cfg, contract_reader=reader, transport=execute)
    assert conn.execute('SELECT state FROM broker_remote_actions').fetchone()[0] == 'unknown'
    assert conn.execute('SELECT count(*) FROM broker_remote_results').fetchone()[0] == 0
    assert run_one(conn, cfg, contract_reader=reader, transport=lambda **_: pytest.fail('rerun'))['state'] == 'occupied'


def test_local_preflight_does_not_require_ssh_or_repo_manifest(config, monkeypatch):
    config.raw['features']['codex'] = True
    config.raw['runtime'].update(remote_transport='local', remote_host='localhost')
    monkeypatch.setattr('k3_support.preflight._resolve_command', lambda value: value)
    calls = []
    def execute(argv, **kw):
        calls.append(argv)
        assert argv[:4] == ['/bin/bash', '--noprofile', '--norc', '-c']
        assert '/.repo' not in argv[4]
        return subprocess.CompletedProcess(argv, 0, '', '')
    result = runtime_doctor(config, check_remote=True, runner=execute)
    assert calls and result['remote']['ready']
    assert 'ssh' not in result['commands'] and result['commands']['local_shell']
