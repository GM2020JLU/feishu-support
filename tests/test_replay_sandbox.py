import pytest

from k3_support.replay_sandbox import run_sandbox, sandbox_command


def roots(tmp_path):
    package = tmp_path / 'k3_support'
    package.mkdir()
    (package / '__init__.py').touch()
    deps = tmp_path / 'site-packages'
    deps.mkdir()
    return package, deps


def test_command_has_no_writable_host_mount_or_network(tmp_path):
    package, deps = roots(tmp_path)
    command = sandbox_command(package=package, site_packages=deps, program='print(1)')
    for flag in ['--unshare-net', '--unshare-pid', '--clearenv', '--die-with-parent']:
        assert flag in command
    assert '--bind' not in command
    assert '--bind-try' not in command
    assert command.count('--ro-bind') in {2, 3}
    assert str(tmp_path) not in command
    assert '-I' in command and '-S' in command


def test_broad_dependency_directory_is_rejected(tmp_path):
    package, _ = roots(tmp_path)
    with pytest.raises(ValueError, match='dependency'):
        sandbox_command(package=package, site_packages=tmp_path, program='print(1)')


def test_supervision_uses_private_stdin_and_resource_limits(tmp_path, monkeypatch):
    package, deps = roots(tmp_path)
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        return '{"ok":true}'

    monkeypatch.setattr('k3_support.replay_sandbox.run_process', runner)
    assert run_sandbox(package=package, site_packages=deps, program='print(1)',
                       request={'private': 'message body'}) == {'ok': True}
    assert calls[0]['env'] == {}
    assert b'message body' in calls[0]['stdin']
    assert 'message body' not in str(calls[0]['argv'])
    assert 'RLIMIT_AS' in calls[0]['argv'][-1]
    assert 'RLIMIT_CORE' in calls[0]['argv'][-1]


@pytest.mark.parametrize('response', ['not-json', '[]', 'null'])
def test_bad_result_is_not_retried(tmp_path, monkeypatch, response):
    package, deps = roots(tmp_path)
    calls = []

    def runner(**kwargs):
        calls.append(1)
        return response

    monkeypatch.setattr('k3_support.replay_sandbox.run_process', runner)
    with pytest.raises((ValueError, TypeError)):
        run_sandbox(package=package, site_packages=deps, program='print(1)', request={})
    assert calls == [1]
