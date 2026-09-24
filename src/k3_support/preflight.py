from __future__ import annotations

import os
import hashlib
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .execution_transport import command_argv

Runner = Callable[..., subprocess.CompletedProcess[str]]


def check_lark_reply_help(executable: str | None, *, runner: Runner = subprocess.run) -> dict[str, Any]:
    """Inspect advertised syntax only; never test authentication or send data."""
    report = {'checked': True, 'ready': False, 'scope': 'advertised_reply_syntax_only',
              'auth_verified': False, 'delivery_verified': False}
    if executable is None:
        return {**report, 'reason': 'command_unavailable'}
    try:
        result = runner([executable, 'im', '+messages-reply', '--help'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**report, 'reason': type(exc).__name__}
    output = result.stdout
    if result.returncode != 0 or not isinstance(output, str) or len(output) > 65536:
        return {**report, 'reason': 'invalid_help_result'}
    usage = re.search(r'(?m)^\s*lark-cli im \+messages-reply \[flags\]\s*$', output)
    missing = [flag for flag in ('--message-id', '--as', '--idempotency-key', '--markdown')
               if not re.search(r'(?m)^\s*' + re.escape(flag) + r'\s', output)]
    return {**report, 'ready': bool(usage) and not missing,
            'reason': 'matched' if usage and not missing else 'reply_syntax_mismatch',
            'missing_flags': missing, 'help_digest': hashlib.sha256(output.encode()).hexdigest()}


def _resolve_command(value: str) -> str | None:
    if "/" in value:
        path = Path(value)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    adjacent = Path(sys.executable).absolute().parent / value
    if adjacent.is_file() and os.access(adjacent, os.X_OK):
        return str(adjacent)
    return shutil.which(value)


def check_codex_exec_help(executable: str | None, *, runner: Runner = subprocess.run) -> dict[str, Any]:
    """Check local advertised syntax without starting a coding session."""
    report = {'checked': True, 'ready': False, 'scope': 'advertised_exec_syntax_only',
              'auth_verified': False, 'execution_verified': False}
    if executable is None:
        return {**report, 'reason': 'command_unavailable'}
    try:
        result = runner([executable, 'exec', '--help'], stdin=subprocess.DEVNULL,
                        capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**report, 'reason': type(exc).__name__}
    output = result.stdout
    if result.returncode != 0 or not isinstance(output, str) or len(output) > 65536:
        return {**report, 'reason': 'invalid_help_result'}
    usage = re.search(r'(?m)^Usage: codex exec \[OPTIONS\] \[PROMPT\]\s*$', output)
    missing = [flag for flag in ('--json', '--output-last-message', '--sandbox', '--model')
               if not re.search(r'(?m)^\s*(?:-\w, )?' + re.escape(flag) + r'(?:\s|$)', output)]
    stdin = bool(re.search(r'instructions are read from stdin', output))
    matched = bool(usage) and not missing and stdin
    return {**report, 'ready': matched, 'reason': 'matched' if matched else 'exec_syntax_mismatch',
            'missing_flags': missing, 'stdin_advertised': stdin,
            'help_digest': hashlib.sha256(output.encode()).hexdigest()}


def runtime_doctor(
    config: Config,
    *,
    check_remote: bool = False,
    check_cli_help: bool = False,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    commands = {
        "hermes": config.runtime("hermes_command"),
        "semantic": config.runtime("semantic_command"),
        "lark_cli": config.runtime("lark_cli_command"),
    }
    remote_required = config.feature("codex") or config.feature("wip_push")
    local_execution = config.raw["runtime"].get("remote_transport", "ssh") == "local"
    if remote_required:
        commands["local_shell" if local_execution else "ssh"] = "/bin/bash" if local_execution else config.runtime("ssh_command")
    if config.feature("codex"):
        commands.update({
            "codex_remote": config.runtime("codex_remote_command"),
            "codex": "codex",
        })
    if config.feature("board"):
        commands.update({
            "serial": config.runtime("serial_command"),
            "codex_board": config.runtime("codex_board_command"),
        })
    resolved = {name: _resolve_command(value) for name, value in commands.items()}
    cli_help = (check_lark_reply_help(resolved['lark_cli'], runner=runner) if check_cli_help
                else {'checked': False, 'ready': None})
    codex_help = (check_codex_exec_help(resolved.get('codex'), runner=runner)
                  if check_cli_help and config.feature('codex') else {'checked': False, 'ready': None})
    problems = [
        f"command is unavailable: {name}"
        for name, value in resolved.items()
        if value is None
    ]
    if check_cli_help and not cli_help['ready']:
        problems.append('Lark reply command syntax check failed')
    if codex_help['checked'] and not codex_help['ready']:
        problems.append('Codex exec command syntax check failed')
    bridge = {'checked': False, 'ready': None, 'inference_verified': False,
              'reason': 'not_requested' if not check_cli_help else 'manifest_not_configured'}
    bridge_path = os.environ.get('K3_SUPPORT_HERMES_BRIDGE_CONFIG')
    if check_cli_help and bridge_path:
        from .hermes_stdin import manifest

        try:
            identity = manifest(bridge_path)
            bridge = {'checked': True, 'ready': True, 'inference_verified': False,
                      'reason': 'source_manifest_matches', 'schema_version': identity['schema_version']}
        except (ValueError, OSError):
            bridge = {'checked': True, 'ready': False, 'inference_verified': False,
                      'reason': 'source_manifest_invalid'}
            problems.append('Hermes bridge source manifest check failed')
    # Keep the virtualenv directory itself. Resolving its Python symlink would
    # compare console scripts with the shared base interpreter directory.
    runtime_bin = Path(sys.executable).absolute().parent
    for name, executable_name in (
        ("codex_remote", "k3-codex-remote"),
        ("codex_board", "k3-codex-board"),
    ):
        value = resolved.get(name)
        if (
            value is not None
            and Path(value).name == executable_name
            and Path(value).absolute().parent != runtime_bin
        ):
            problems.append(f"command belongs to a different installed release: {name}")
    scripts: dict[str, dict[str, Any]] = {}
    for name in ("board_control_script", "board_boot_script") if config.feature("board") else ():
        path = Path(config.runtime(name))
        ready = path.is_file() and not path.is_symlink() and os.access(path, os.R_OK)
        scripts[name] = {"path": str(path), "ready": ready}
        if not ready:
            problems.append(f"board script is unavailable or unsafe: {name}")

    remote: dict[str, Any] = {"checked": False, "ready": None}
    if check_remote and not remote_required:
        remote["reason"] = "remote execution capabilities are disabled"
    if check_remote and remote_required:
        checks = [
            "set -eu",
            *(
                f"command -v {name} >/dev/null"
                for name in (
                    ("bwrap", "bash", "git", "realpath", "sha256sum")
                    if config.feature("codex")
                    else ("bash", "git", "realpath", "sha256sum")
                )
            ),
        ]
        remote_paths = {
            config.runtime("remote_workspace_root"),
            config.runtime("remote_source_root"),
            *(item["path"] for item in config.raw["repositories"].values()),
        }
        # The SSH login shell may provide a POSIX `test` builtin that rejects
        # GNU-style `--`. Config validation already guarantees absolute safe
        # paths, so no option terminator is needed here.
        checks.extend(f"test -d {shlex.quote(path)}" for path in sorted(remote_paths))
        workspace_root = shlex.quote(config.runtime("remote_workspace_root"))
        worktree_root = shlex.quote(config.runtime("remote_worktree_root"))
        checks.extend(
            (
                f"test -w {workspace_root}",
                (
                    f"test ! -e {worktree_root} || "
                    f"{{ test -d {worktree_root} && "
                    f"test -w {worktree_root}; }}"
                ),
            )
        )
        if resolved["local_shell" if local_execution else "ssh"] is None:
            remote = {"checked": True, "ready": False, "returncode": None}
            problems.append("execution preflight requires a local shell" if local_execution else "remote preflight requires the configured SSH command")
        else:
            try:
                result = runner(
                    command_argv(config, "; ".join(checks)),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                remote = {
                    "checked": True,
                    "ready": result.returncode == 0,
                    "returncode": result.returncode,
                }
            except (OSError, subprocess.TimeoutExpired) as exc:
                remote = {
                    "checked": True, "ready": False, "returncode": None,
                    "error_class": type(exc).__name__,
                }
            if not remote["ready"]:
                problems.append("remote build-host preflight failed")
    return {
        "ready": not problems,
        "commands": resolved,
        "scripts": scripts,
        "remote": remote,
        "cli_help": cli_help,
        "codex_help": codex_help,
        "bridge_manifest": bridge,
        "required_capabilities": [
            "chat_control", *(name for name in ("codex", "board", "wip_push") if config.feature(name))
        ],
        "problems": problems,
    }
