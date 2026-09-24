import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from k3_support.broker_service import main


def test_service_resolves_worker_account_before_running(monkeypatch):
    seen = []
    monkeypatch.setattr("k3_support.broker_service.pwd.getpwnam", lambda name: SimpleNamespace(pw_uid=4242))
    monkeypatch.setattr("k3_support.broker_service.run", lambda **kwargs: seen.append(kwargs))
    assert main(["--config", "synthetic", "--key-directory", "synthetic", "--worker-user", "test-worker",
                 "--contract-directory", "synthetic-contract"]) == 0
    assert seen[0]["contract_directory"] == "synthetic-contract"
    assert seen[0]["worker_uid"] == 4242


def test_systemd_template_syntax_with_existing_fixture_executable(tmp_path):
    source = Path(__file__).resolve().parents[1] / "config/systemd-broker"
    names = ("k3-support-broker.service", "k3-support-broker.socket", "k3-support-broker-worker@.service",
             "k3-support-broker-observer.service", "k3-support-broker-dispatcher.service",
             "k3-support-broker-launcher.service", "k3-support-broker-launcher.socket",
             "k3-support-broker-remote.service", "k3-support-broker-board.service")
    for name in names:
        value = (source / name).read_text()
        # Syntax check only: production executable has not been installed.
        value = value.replace("/opt/k3-support/venv/bin/k3-support-broker-worker", sys.executable)
        value = value.replace("/opt/k3-support/venv/bin/k3-support-broker-launcher", sys.executable)
        value = value.replace("/opt/k3-support/venv/bin/k3-support-broker-observer", sys.executable)
        value = value.replace("/opt/k3-support/venv/bin/k3-support-broker-remote", sys.executable)
        value = value.replace("/opt/k3-support/venv/bin/k3-support-broker-board", sys.executable)
        value = value.replace("/opt/k3-support/venv/bin/k3-support-broker", sys.executable)
        (tmp_path / name).write_text(value)
    result = subprocess.run(["systemd-analyze", "verify", *[str(tmp_path / name) for name in names]],
                            capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr


def test_worker_template_never_restarts_unknown_execution():
    source = Path(__file__).resolve().parents[1] / "config/systemd-broker/k3-support-broker-worker@.service"
    value = source.read_text()
    assert "User=k3-support-worker" in value
    assert "RemainAfterExit=yes" in value and "Slice=system.slice" in value
    assert "Restart=no" in value and "KillMode=control-group" in value
    assert "--claim-request-id %i" in value
    assert "--config " not in value and "broker.key" not in value


def test_board_service_denies_devices_until_explicit_deployment_allowlist():
    source = Path(__file__).resolve().parents[1] / "config/systemd-broker/k3-support-broker-board.service"
    directives = {line.split("=", 1)[0]: line.split("=", 1)[1]
                  for line in source.read_text().splitlines() if line and not line.startswith("#") and "=" in line}
    assert directives["User"] == "k3-support-control"
    assert directives["DevicePolicy"] == "closed" and "DeviceAllow" not in directives
    assert directives["PrivateDevices"] == "no"
    assert directives["ProtectHome"] == "yes" and directives["ProtectSystem"] == "strict"
    assert directives["CapabilityBoundingSet"] == ""
    assert directives["KillMode"] == "control-group"
    assert "--worker-user k3-support-worker --watch" in directives["ExecStart"]


def test_each_native_agent_profile_preserves_supervision_and_parses(tmp_path):
    import shlex

    source = Path(__file__).resolve().parents[1] / 'config/systemd-broker'
    base = (source / 'k3-support-broker-worker@.service').read_text()
    assert '--agent-executable ${K3_SUPPORT_AGENT_EXECUTABLE}' in base
    assert '--agent-home ${K3_SUPPORT_AGENT_HOME}' in base
    expected = {'codex':'/opt/codex/bin/codex', 'claude':'/opt/claude/bin/claude',
                'dsh':'/opt/dsh/bin/dsh', 'opencode':'/opt/opencode/bin/opencode',
                'hermes':'/opt/hermes/venv/bin/python'}
    for agent, executable in expected.items():
        profile = (source/'agent-profiles'/f'{agent}.conf').read_text()
        settings = [line.split('=',1) for line in profile.splitlines() if line and not line.startswith(('#','['))]
        assert {key for key, _ in settings} == {'Environment','StateDirectory'}
        environment = dict(value.split('=',1) for key,value in settings if key == 'Environment')
        assert environment == {'K3_SUPPORT_AGENT_EXECUTABLE':executable,
                               'K3_SUPPORT_AGENT_HOME':f'/var/lib/k3-support-worker/{agent}'}
        command = next(line.split('=',1)[1] for line in base.splitlines() if line.startswith('ExecStart='))
        for key, value in environment.items():
            command = command.replace('${'+key+'}', value)
        argv = shlex.split(command)
        assert argv[argv.index('--agent-executable')+1] == executable
        assert argv[argv.index('--agent-home')+1] == f'/var/lib/k3-support-worker/{agent}'
        combined = base.replace('/opt/k3-support/venv/bin/k3-support-broker-worker',sys.executable)+'\n'+profile
        path = tmp_path/f'profile-{agent}@.service'
        # Dependency files are supplied too; this is syntax, not a live deployment.
        (tmp_path/'k3-support-broker.socket').write_text((source/'k3-support-broker.socket').read_text())
        (tmp_path/'k3-support-broker.service').write_text((source/'k3-support-broker.service').read_text().replace('/opt/k3-support/venv/bin/k3-support-broker',sys.executable))
        path.write_text(combined)
        result = subprocess.run(['systemd-analyze','verify',str(path)],capture_output=True,text=True,timeout=10,check=False)
        assert result.returncode == 0, result.stderr


def test_catalog_dropins_use_shared_directory_and_preserve_base_isolation(tmp_path):
    import re
    import shlex

    source = Path(__file__).resolve().parents[1] / 'config/systemd-broker'
    names = []
    for path in source.glob('*.service'):
        value = path.read_text()
        dropin = source / 'catalog-mode' / (path.name + '.conf')
        if dropin.exists():
            extra = dropin.read_text()
            command = next(line.split('=', 1)[1] for line in extra.splitlines()
                           if line.startswith('ExecStart=') and line != 'ExecStart=')
            arguments = shlex.split(command)
            assert '--execution-catalog' in arguments
            assert arguments[arguments.index('--contract-directory') + 1] == '/etc/k3-support/execution-catalog'
            assert 'User=' not in extra and 'Restart=' not in extra and 'ProtectSystem=' not in extra
            if 'worker@' in path.name:
                assert '--agent-executable' not in arguments and '--agent-home' not in arguments
            value += '\n' + extra
        value = re.sub(r'/opt/k3-support/venv/bin/k3-support-broker[-a-z]*', sys.executable, value)
        (tmp_path / path.name).write_text(value)
        names.append(str(tmp_path / path.name))
    for path in source.glob('*.socket'):
        (tmp_path / path.name).write_text(path.read_text())
    result = subprocess.run(['systemd-analyze', 'verify', *names], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
