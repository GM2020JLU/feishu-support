"""Minimal-root remote sandbox policy, independent of CLI/config loading.

The mount list is policy, not a complete authorization boundary.  In particular,
the remote launcher must eventually run under the independent broker identity;
the canonical-path preflight cannot defeat a hostile same-UID rename race.
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

SYSTEM_TOOLCHAIN_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64")
_PRIVATE_ROOTS = ("/etc", "/run", "/proc", "/sys", "/dev", "/tmp", "/var", "/root")
_PRIVATE_COMPONENTS = {
    ".ssh",
    ".gnupg",
    ".config",
    ".hermes",
    ".codex",
    ".git-credentials",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".password-store",
    ".pki",
}


def _path(value: object, label: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or len(value) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{label} must be a canonical absolute path")
    path = PurePosixPath(value)
    if str(path) != value or ".." in path.parts or path == PurePosixPath("/"):
        raise ValueError(f"{label} must be a bounded canonical absolute path")
    return path


def _overlaps(left: PurePosixPath, right: PurePosixPath) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _project_path(value: object, label: str) -> PurePosixPath:
    path = _path(value, label)
    if (
        len(path.parts) < 3
        or any(part in _PRIVATE_COMPONENTS for part in path.parts)
        or any(
            _overlaps(path, PurePosixPath(root))
            for root in (*SYSTEM_TOOLCHAIN_ROOTS, *_PRIVATE_ROOTS)
        )
    ):
        raise ValueError(f"{label} overlaps a system/private root")
    return path


def validate_toolchain_roots(
    value: object, *, source_root: str, worktree_root: str
) -> list[str]:
    """Validate trusted *remote* roots; no local FS assumptions or env grants.

    A canonical-path check on the remote host also runs before each mount.  This
    function deliberately does not resolve remote paths on the control host.
    """
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("remote_toolchain_roots must be a list of at most 16 paths")
    source = _project_path(source_root, "remote_source_root")
    worktrees = _project_path(worktree_root, "remote_worktree_root")
    result: list[str] = []
    for item in value:
        root = _project_path(item, "remote_toolchain_roots entry")
        if (
            (root.parts[1] == "home" and len(root.parts) < 4)
            or any(part.startswith(".") for part in root.parts[1:])
            or any(_overlaps(root, other) for other in (source, worktrees))
            or any(_overlaps(root, PurePosixPath(other)) for other in result)
        ):
            raise ValueError(
                "remote_toolchain_roots entry is broad, private, or overlapping"
            )
        result.append(str(root))
    return result


def sandbox_argv(
    *,
    case_id: str,
    source_root: str,
    worktree_root: str,
    repo_paths: list[str],
    toolchain_roots: list[str],
    writable: bool,
    command: str,
    work_id: str | None = None,
    seed_work_id: str | None = None,
    seed_work_ids: list[str] | None = None,
) -> list[str]:
    """Build bwrap argv from the operator's whitelist, never a host-root bind."""
    if type(writable) is not bool:
        raise ValueError("writable must be an explicit boolean")
    if not isinstance(repo_paths, list) or len(repo_paths) > 500:
        raise ValueError("repo_paths must be a bounded list")
    if not isinstance(case_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", case_id
    ):
        raise ValueError("Case ID must be one safe path component")
    if (
        not isinstance(command, str)
        or not command
        or "\x00" in command
        or len(command.encode()) > 32768
    ):
        raise ValueError("remote command is empty or too large")
    source = _project_path(source_root, "remote_source_root")
    worktrees = _project_path(worktree_root, "remote_worktree_root")
    if _overlaps(source, worktrees):
        raise ValueError("remote source and worktree roots overlap")
    extras = validate_toolchain_roots(
        toolchain_roots, source_root=source_root, worktree_root=worktree_root
    )
    repositories: list[str] = []
    for item in repo_paths:
        repo = _path(item, "repository")
        if (
            not repo.is_relative_to(source)
            or repo == source
            or any(part.startswith(".") for part in repo.relative_to(source).parts)
        ):
            raise ValueError("repository must be strictly below remote_source_root")
        if any(_overlaps(repo, PurePosixPath(other)) for other in repositories):
            raise ValueError("repository roots must not overlap")
        repositories.append(str(repo))
    argv = [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-net",
        "--unshare-uts",
        "--cap-drop",
        "ALL",
        "--clearenv",
    ]
    # bwrap starts with an empty root.  Compatibility paths can be symlinks on a
    # merged-/usr host; binding their contents also supports non-merged hosts.
    for root in SYSTEM_TOOLCHAIN_ROOTS:
        argv.extend(("--ro-bind-try", root, root))
    argv.extend(
        ("--dir", "/etc", "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache")
    )
    for root in extras:
        argv.extend(("--ro-bind", root, root))
    argv.extend(("--tmpfs", "/tmp", "--dir", "/tmp/home"))
    # Parent directories are synthetic.  Their host contents are never mounted.
    argv.extend(("--dir", source_root, "--dir", worktree_root))
    metadata = str(source / ".repo")
    argv.extend(("--ro-bind-try", metadata, metadata))
    for repo in repositories:
        argv.extend(("--ro-bind", repo, repo))
    case_root = str(worktrees / case_id)
    if work_id is not None:
        if not writable or not isinstance(work_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", work_id):
            raise ValueError("invalid isolated job directory")
        case_root = str(PurePosixPath(case_root) / ("investigation-" + work_id))
    if seed_work_ids is not None and (not isinstance(seed_work_ids, list) or len(seed_work_ids) > 20):
        raise ValueError("invalid candidate job directories")
    seeds = ([seed_work_id] if seed_work_id is not None else []) + (seed_work_ids or [])
    seen_seeds = set()
    for seed_id in seeds:
        if (work_id is None or seed_id == work_id or not isinstance(seed_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", seed_id)):
            raise ValueError("invalid candidate job directory")
        if seed_id in seen_seeds:
            continue
        seen_seeds.add(seed_id)
        seed = str(worktrees / case_id / ("investigation-" + seed_id) / "repository")
        argv.extend(("--ro-bind", seed, seed))
    if writable:
        argv.extend(("--bind", case_root, case_root))
    argv.extend(("--dev", "/dev", "--proc", "/proc"))
    environment = {
        "HOME": "/tmp/home",
        "TMPDIR": "/tmp",
        "PATH": ":".join(
            [
                *(path for root in extras for path in (root + "/bin", root)),
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            ]
        ),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "K3_SUPPORT_CASE": case_id,
    }
    for key, value in environment.items():
        argv.extend(("--setenv", key, value))
    argv.extend(
        (
            "--chdir",
            case_root if writable else source_root,
            "--",
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            command,
        )
    )
    return argv


def render_remote_command(argv: list[str], *, writable: bool, nested_work: bool = False) -> str:
    """Reject symlinked grant roots before creating/binding the Case directory.

    Require the deployment-created worktree parent, rather than mkdir -p through
    arbitrary host parents.  All shell values are quoted.  Symlinks *inside* a
    mounted repository still resolve only against the minimal sandbox root.
    """
    if type(writable) is not bool:
        raise ValueError("writable must be an explicit boolean")
    checks: list[str] = []
    for index, arg in enumerate(argv[: argv.index("--")]):
        if arg not in {"--ro-bind", "--ro-bind-try", "--bind"}:
            continue
        path = argv[index + 1]
        if path in SYSTEM_TOOLCHAIN_ROOTS or path == "/etc/ld.so.cache":
            continue
        quoted = shlex.quote(path)
        check = f'test "$(/usr/bin/realpath -e -- {quoted})" = {quoted} && test -d {quoted}'
        if arg == "--ro-bind-try":
            check = f"{{ {{ test ! -e {quoted} && test ! -L {quoted}; }} || {{ {check}; }}; }}"
        if arg == "--bind":
            if not writable:
                raise ValueError("read-only sandbox cannot contain a writable bind")
            parent = shlex.quote(str(PurePosixPath(path).parent))
            if nested_work:
                grandparent = shlex.quote(str(PurePosixPath(path).parent.parent))
                checks.extend((
                    f'test "$(/usr/bin/realpath -e -- {grandparent})" = {grandparent}',
                    f"test -d {grandparent}",
                    f"{{ test -d {parent} || /usr/bin/mkdir -m 700 -- {parent}; }}",
                ))
            checks.extend(
                (
                    f'test "$(/usr/bin/realpath -e -- {parent})" = {parent}',
                    f"test -d {parent}",
                    f"{{ test -d {quoted} || /usr/bin/mkdir -m 700 -- {quoted}; }}",
                )
            )
        checks.append(check)
    checks.append("exec " + shlex.join(argv))
    return " && ".join(checks)
