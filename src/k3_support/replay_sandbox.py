"""Read-only, networkless Linux namespace for trusted replay Python code.

No source database or home directory is mounted. Supply snapshot/scenario data
over stdin. This command builder does not enforce an execution deadline; its
supervisor must terminate/reap the namespace on timeout or protocol failure.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .broker_process import run_process
from .remote_sandbox import SYSTEM_TOOLCHAIN_ROOTS


def run_sandbox(*, package: Path, site_packages: Path, program: str,
                request: dict, timeout: float = 30, output_limit: int = 1048576) -> dict:
    """Run trusted replay code with bounded JSON pipes and no inherited secrets.

    Reuses broker supervision; the PID namespace additionally prevents escaped
    child sessions from outliving the sandbox init. Never retries failed runs.
    """
    if not isinstance(request, dict):
        raise TypeError('replay request must be an object')
    payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode()
    bootstrap = (
        'import resource\n'
        'resource.setrlimit(resource.RLIMIT_AS, (1073741824, 1073741824))\n'
        'resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n'
        + program
    )
    output = run_process(
        argv=sandbox_command(package=package, site_packages=site_packages, program=bootstrap),
        cwd='/', env={}, stdin=payload, heartbeat=lambda: None,
        timeout=timeout, heartbeat_interval=min(1, timeout), output_limit=output_limit,
    )
    try:
        result = json.loads(output)
    except (ValueError, TypeError) as exc:
        raise ValueError('replay returned invalid JSON') from exc
    if not isinstance(result, dict):
        raise TypeError('replay result must be an object')
    return result


def sandbox_command(*, package: Path, site_packages: Path, program: str) -> list[str]:
    """Mount only the application package and selected dependency directory.

    These paths are trusted deployment inputs, never model/scenario fields.
    Program is trusted application code, not user-supplied Python.
    """
    package = package.resolve(strict=True)
    site_packages = site_packages.resolve(strict=True)
    if package.name != 'k3_support' or not (package / '__init__.py').is_file():
        raise ValueError('expected the k3_support package directory')
    if site_packages.name not in {'site-packages', 'dist-packages'} or not site_packages.is_dir():
        raise ValueError('expected a Python dependency directory')
    if not isinstance(program, str) or not program or len(program.encode()) > 65536 or '\0' in program:
        raise ValueError('invalid replay bootstrap')
    argv = ['/usr/bin/bwrap', '--die-with-parent', '--new-session',
            '--unshare-user', '--unshare-pid', '--unshare-ipc', '--unshare-net',
            '--unshare-uts', '--cap-drop', 'ALL', '--clearenv']
    for root in SYSTEM_TOOLCHAIN_ROOTS:
        argv.extend(['--ro-bind-try', root, root])
    # Match native dependency ABI to this process, not the host system Python.
    interpreter = Path(sys.executable).resolve(strict=True)
    runtime = Path(sys.base_prefix).resolve(strict=True)
    if not interpreter.is_relative_to(runtime):
        raise ValueError('interpreter is outside its base runtime')
    if runtime == Path('/'):
        raise ValueError('refusing a host-root runtime mount')
    if not interpreter.is_relative_to(Path('/usr')):
        argv.extend(['--ro-bind', str(runtime), str(runtime)])
    argv.extend(['--dir', '/etc', '--ro-bind-try', '/etc/ld.so.cache', '/etc/ld.so.cache',
                 '--dir', '/replay', '--ro-bind', str(package), '/replay/k3_support',
                 '--ro-bind', str(site_packages), '/replay/deps',
                 '--tmpfs', '/tmp', '--dir', '/tmp/home', '--setenv', 'HOME', '/tmp/home',
                 '--proc', '/proc', '--dev', '/dev',
                 '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
                 '--chdir', '/tmp', '--', str(interpreter), '-I', '-S', '-c',
                 'import sys; sys.dont_write_bytecode=True; '
                 'sys.path[:0]=["/replay", "/replay/deps"];\n' + program])
    return argv
