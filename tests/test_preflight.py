from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from k3_support.config import Config, validate_config
from k3_support.preflight import runtime_doctor


@pytest.mark.parametrize('mode', ['missing', 'valid', 'invalid'])
def test_bridge_source_check_never_certifies_inference(config, tmp_path, monkeypatch, mode):
    cfg = portable_config(config, tmp_path)
    monkeypatch.setattr('k3_support.preflight._resolve_command', lambda value: value)
    for name in ['check_lark_reply_help', 'check_codex_exec_help']:
        monkeypatch.setattr('k3_support.preflight.' + name, lambda *a, **k: {'checked':True,'ready':True})
    monkeypatch.delenv('K3_SUPPORT_HERMES_BRIDGE_CONFIG', raising=False)
    if mode != 'missing':
        monkeypatch.setenv('K3_SUPPORT_HERMES_BRIDGE_CONFIG', '/private/manifest.json')
    def check(path):
        assert mode != 'missing'
        if mode == 'invalid':
            raise ValueError('PRIVATE HASH ERROR')
        return {'schema_version': 2}
    monkeypatch.setattr('k3_support.hermes_stdin.manifest', check)
    report = runtime_doctor(cfg, check_cli_help=True)
    assert report['bridge_manifest']['checked'] == (mode != 'missing')
    assert report['bridge_manifest']['ready'] == {'missing':None,'valid':True,'invalid':False}[mode]
    assert not report['bridge_manifest']['inference_verified']
    assert 'PRIVATE' not in str(report) and '/private/' not in str(report)
    assert report['ready'] == (mode != 'invalid')


@pytest.mark.parametrize('kind', ['valid', 'parent', 'missing_flag', 'failed', 'timeout'])
def test_reply_help_requires_exact_usage_and_flags_without_auth(kind):
    from k3_support.preflight import check_lark_reply_help

    def runner(argv, **kwargs):
        assert argv == ['/fixture/lark', 'im', '+messages-reply', '--help']
        assert kwargs['timeout'] == 10 and kwargs['stdin'] == subprocess.DEVNULL
        if kind == 'timeout':
            raise subprocess.TimeoutExpired(argv, 10, output='PRIVATE ERROR')
        output = 'Usage:\n lark-cli im +messages-reply [flags]\n'
        output += '\n'.join('  ' + flag + ' string' for flag in
                            ['--message-id', '--as', '--idempotency-key', '--markdown'])
        if kind == 'parent':
            output = output.replace('+messages-reply [flags]', '[command]')
        if kind == 'missing_flag':
            output = output.replace('--idempotency-key', '--unrelated')
        return subprocess.CompletedProcess(argv, int(kind == 'failed'), output, 'PRIVATE ERROR')

    result = check_lark_reply_help('/fixture/lark', runner=runner)
    assert result['ready'] == (kind == 'valid')
    assert not result['auth_verified'] and not result['delivery_verified']
    assert 'PRIVATE' not in str(result)


def portable_config(config, tmp_path: Path) -> Config:
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o700)
    board_control = tmp_path / "board-ctrl.sh"
    board_control.write_text("#!/bin/sh\nexit 0\n")
    board_boot = tmp_path / "ram-boot.sh"
    board_boot.write_text("#!/bin/sh\nexit 0\n")
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"].update({"codex": True, "board": True})
    raw["runtime"].update(
        {
            "remote_host": "builder@k3-host",
            "remote_workspace_root": "/srv/firmware",
            "remote_source_root": "/srv/firmware/source",
            "remote_worktree_root": "/srv/firmware/worktrees",
            "ssh_command": str(tool),
            "semantic_command": str(tool),
            "serial_command": str(tool),
            "codex_remote_command": str(tool),
            "codex_board_command": str(tool),
            "board_control_script": str(board_control),
            "board_boot_script": str(board_boot),
        }
    )
    raw["repositories"] = {
        "u-boot": {
            "path": "/srv/firmware/source/u-boot",
            "remote": "origin",
            "base_branch": "main",
        }
    }
    return Config(validate_config(raw), config.path)


@pytest.mark.parametrize('kind', ['valid', 'parent', 'missing_flag', 'stdin', 'failed', 'timeout'])
def test_codex_help_checks_syntax_without_starting_session(kind):
    from k3_support.preflight import check_codex_exec_help

    def runner(argv, **kwargs):
        assert argv == ['/fixture/codex', 'exec', '--help']
        assert kwargs['stdin'] == subprocess.DEVNULL and kwargs['timeout'] == 10
        if kind == 'timeout':
            raise subprocess.TimeoutExpired(argv, 10, output='PRIVATE')
        output = ('Usage: codex exec [OPTIONS] [PROMPT]\n'
                  ' instructions are read from stdin\n'
                  ' --json\n -o, --output-last-message <FILE>\n'
                  ' -s, --sandbox <MODE>\n -m, --model <MODEL>\n')
        if kind == 'parent':
            output = output.replace('codex exec', 'codex')
        if kind == 'missing_flag':
            output = output.replace('--json', '--other')
        if kind == 'stdin':
            output = output.replace('instructions are read from stdin', 'prompt argument only')
        return subprocess.CompletedProcess(argv, int(kind == 'failed'), output, 'PRIVATE')

    result = check_codex_exec_help('/fixture/codex', runner=runner)
    assert result['ready'] == (kind == 'valid')
    assert not result['execution_verified'] and not result['auth_verified']
    assert 'PRIVATE' not in str(result)


@pytest.mark.parametrize('enabled,requested,compatible', [
    (True, True, True), (True, True, False), (False, True, False), (True, False, False),
])
def test_runtime_help_gate_respects_feature_and_explicit_opt_in(
    config, tmp_path, monkeypatch, enabled, requested, compatible
):
    cfg = portable_config(config, tmp_path)
    cfg.raw['features']['codex'] = enabled
    monkeypatch.setattr('k3_support.preflight._resolve_command', lambda value: value)
    calls = []

    def lark_help(executable, **kwargs):
        calls.append('lark')
        return {'checked': True, 'ready': True}

    def codex_help(executable, **kwargs):
        assert executable == 'codex'
        calls.append('codex')
        return {'checked': True, 'ready': compatible}

    monkeypatch.setattr('k3_support.preflight.check_lark_reply_help', lark_help)
    monkeypatch.setattr('k3_support.preflight.check_codex_exec_help', codex_help)
    result = runtime_doctor(cfg, check_cli_help=requested)
    assert calls == (['lark'] + (['codex'] if enabled else []) if requested else [])
    assert result['ready'] == (not (enabled and requested) or compatible)
    assert ('Codex exec command syntax check failed' in result['problems']) == (enabled and requested and not compatible)
    assert result['remote']['checked'] is False


def test_runtime_doctor_checks_local_dependencies_without_remote_side_effect(
    config, tmp_path, monkeypatch
):
    cfg = portable_config(config, tmp_path)
    monkeypatch.setattr(
        "k3_support.preflight.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    result = runtime_doctor(cfg)
    assert result["ready"] is True
    assert result["remote"] == {"checked": False, "ready": None}


def test_runtime_doctor_remote_check_uses_configured_host_and_safe_script(
    config, tmp_path, monkeypatch
):
    cfg = portable_config(config, tmp_path)
    monkeypatch.setattr(
        "k3_support.preflight.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = runtime_doctor(cfg, check_remote=True, runner=runner)
    assert result["ready"] is True
    assert calls[0][0][:2] == [str(tmp_path / "tool"), "builder@k3-host"]
    assert "test -d /srv/firmware/source/u-boot" in calls[0][0][2]
    assert "test -d --" not in calls[0][0][2]
    assert calls[0][1]["timeout"] == 30


def test_runtime_doctor_fails_closed_for_missing_or_symlinked_local_tools(
    config, tmp_path, monkeypatch
):
    cfg = portable_config(config, tmp_path)
    missing = tmp_path / "missing"
    cfg.raw["runtime"]["serial_command"] = str(missing)
    target = Path(cfg.runtime("board_control_script"))
    target.unlink()
    real = tmp_path / "real-board.sh"
    real.write_text("#!/bin/sh\n")
    target.symlink_to(real)
    monkeypatch.setattr("k3_support.preflight.shutil.which", lambda name: None)
    result = runtime_doctor(cfg)
    assert result["ready"] is False
    assert "command is unavailable: serial" in result["problems"]
    assert (
        "board script is unavailable or unsafe: board_control_script"
        in result["problems"]
    )


def test_runtime_doctor_rejects_codex_wrappers_from_another_release(
    config, tmp_path, monkeypatch
):
    cfg = portable_config(config, tmp_path)
    old_bin = tmp_path / "old-release" / "bin"
    old_bin.mkdir(parents=True)
    for name in ("k3-codex-remote", "k3-codex-board"):
        command = old_bin / name
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o700)
    cfg.raw["runtime"]["codex_remote_command"] = str(old_bin / "k3-codex-remote")
    cfg.raw["runtime"]["codex_board_command"] = str(old_bin / "k3-codex-board")
    monkeypatch.setattr(
        "k3_support.preflight.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    result = runtime_doctor(cfg)

    assert result["ready"] is False
    assert (
        "command belongs to a different installed release: codex_remote"
        in result["problems"]
    )
    assert (
        "command belongs to a different installed release: codex_board"
        in result["problems"]
    )


def test_chat_mail_only_checks_no_executor_tools_or_remote(config, monkeypatch):
    raw = copy.deepcopy(config.raw)
    raw["schema_version"] = 2
    raw.pop("runtime")
    raw["features"]["mail"] = True
    cfg = Config(validate_config(raw), config.path)
    calls = []

    def resolve(command):
        calls.append(command)
        if command == cfg.runtime("semantic_command"):
            return command
        return f"/opt/office/{command}" if command in {"hermes", "lark-cli"} else None

    def no_remote(*args, **kwargs):
        raise AssertionError("disabled executors must not contact a build host")

    monkeypatch.setattr("k3_support.preflight._resolve_command", resolve)
    result = runtime_doctor(cfg, check_remote=True, runner=no_remote)
    assert result["ready"] is True
    assert calls == ["hermes", cfg.runtime("semantic_command"), "lark-cli"]
    assert result["scripts"] == {}
    assert result["remote"]["checked"] is False
    assert result["required_capabilities"] == ["chat_control"]


def test_push_only_does_not_require_board_codex_or_repo_tool(config, tmp_path, monkeypatch):
    cfg = portable_config(config, tmp_path)
    cfg.raw["features"].update({"codex": False, "board": False, "wip_push": True})
    monkeypatch.setattr("k3_support.preflight._resolve_command", lambda value: value)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = runtime_doctor(cfg, check_remote=True, runner=runner)
    assert result["ready"] is True
    assert set(result["commands"]) == {"hermes", "semantic", "lark_cli", "ssh"}
    assert result["scripts"] == {}
    assert "bwrap" not in calls[0][2]
    assert "/.repo" not in calls[0][2]


def test_remote_doctor_reports_timeout_without_exposing_output(config, tmp_path, monkeypatch):
    cfg = portable_config(config, tmp_path)
    monkeypatch.setattr("k3_support.preflight._resolve_command", lambda value: value)

    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30, output="private remote output")

    result = runtime_doctor(cfg, check_remote=True, runner=runner)
    assert result["ready"] is False
    assert result["remote"]["error_class"] == "TimeoutExpired"
    assert "private remote output" not in repr(result)
