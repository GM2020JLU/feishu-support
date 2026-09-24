from __future__ import annotations

import copy
import shlex
from pathlib import Path

import pytest

from k3_support.codex_remote import (
    RemoteSandboxError,
    _case_allows_work,
    build_remote_command,
)
from k3_support.config import Config, validate_config
from k3_support.executors import create_codex_job
from k3_support.remote_sandbox import sandbox_argv, validate_toolchain_roots
from k3_support.store import create_case, transition_case


def _config(config):
    raw = copy.deepcopy(config.raw)
    raw["repositories"] = {
        "u-boot": {
            "path": "/data/home2/operator/WorkSpace/k3/uboot-2022.10",
            "remote": "origin",
            "base_branch": "main",
        },
        "edk2-platforms": {
            "path": "/data/home2/operator/WorkSpace/k3/tianocore/edk2-platforms",
            "remote": "origin",
            "base_branch": "main",
        },
    }
    return Config(validate_config(raw), config.path)


def test_inspect_sandbox_is_read_only_hides_home_and_has_no_network(config):
    command = build_remote_command(
        _config(config),
        case_id="K3-20260901-0001",
        mode="inspect",
        repo_name=None,
        command="git -C /data/home2/operator/WorkSpace/k3/uboot-2022.10 status --short",
    )
    assert "--unshare-net" in command
    assert "--ro-bind / /" not in command
    assert "--tmpfs /data/home2/operator" not in command
    assert "--clearenv" in command
    assert "--unshare-pid" in command
    assert "--unshare-ipc" in command
    assert "--new-session" in command
    assert "--ro-bind /data/home2/operator/WorkSpace/k3/uboot-2022.10" in command
    assert "--bind /data/home2/operator/WorkSpace/k3/uboot-2022.10" not in command


def test_work_sandbox_keeps_sources_read_only_and_writes_only_case_clone(config):
    command = build_remote_command(
        _config(config),
        case_id="K3-20260901-0002",
        mode="work",
        repo_name="u-boot",
        command="git status --short",
    )
    assert "--ro-bind-try /data/home2/operator/WorkSpace/k3/.repo" in command
    assert "--bind /data/home2/operator/WorkSpace/k3/uboot-2022.10" not in command
    assert (
        "--bind /data/home2/operator/WorkSpace/k3/tianocore/edk2-platforms" not in command
    )
    assert "/data/home2/operator/WorkSpace/k3-ai-worktrees/K3-20260901-0002" in command


@pytest.mark.parametrize("workspace", ["/srv/firmware", "/home/test-operator/firmware"])
def test_remote_sandbox_uses_configured_portable_roots(config, workspace):
    raw = copy.deepcopy(config.raw)
    raw["runtime"].update(
        {
            "remote_workspace_root": workspace,
            "remote_source_root": workspace + "/k3",
            "remote_worktree_root": workspace + "/case-worktrees",
        }
    )
    raw["repositories"] = {
        "u-boot": {
            "path": workspace + "/k3/u-boot",
            "remote": "origin",
            "base_branch": "main",
        }
    }
    cfg = Config(validate_config(raw), config.path)
    command = build_remote_command(
        cfg,
        case_id="K3-20260901-0003",
        mode="work",
        repo_name="u-boot",
        command="git status --short",
    )
    assert "--ro-bind / /" not in command
    assert "--ro-bind " + workspace + "/k3/u-boot" in command
    assert workspace + "/case-worktrees/K3-20260901-0003" in command
    assert "/data/home2/operator" not in command


def _argv(**changes):
    arguments = {
        "case_id": "CASE-1",
        "source_root": "/srv/project/source",
        "worktree_root": "/srv/project/worktrees",
        "repo_paths": ["/srv/project/source/u-boot"],
        "toolchain_roots": [],
        "writable": False,
        "command": "git status --short",
    }
    arguments.update(changes)
    return sandbox_argv(**arguments)


@pytest.mark.parametrize(
    "case_id", ["..", ".", "../CASE-1", "/tmp/escape", "case/x", "a\n", ""]
)
def test_case_path_cannot_escape_grant(case_id):
    with pytest.raises(ValueError, match="Case ID"):
        _argv(case_id=case_id, writable=True)


@pytest.mark.parametrize(
    "root",
    [
        "/",
        "/home",
        "/home/alice",
        "/etc",
        "/etc/ssl",
        "/run",
        "/run/user/1",
        "/proc",
        "/proc/1/root",
        "/dev",
        "/sys",
        "/root",
        "/var/lib",
        "/tmp/sdk",
        "/usr",
        "/usr/local/toolchain",
        "/opt",
        "/opt/../home",
        "/opt//sdk",
        "/opt/sdk/",
        "/home/alice/.ssh",
        "/srv/project/source",
        "/srv/project/source/sdk",
        "/srv/project/worktrees",
        "/srv/project/worktrees/CASE-1",
        "/srv/project",
    ],
)
def test_toolchain_whitelist_rejects_broad_private_and_overlapping_roots(root):
    with pytest.raises(ValueError):
        validate_toolchain_roots(
            [root],
            source_root="/srv/project/source",
            worktree_root="/srv/project/worktrees",
        )


@pytest.mark.parametrize(
    "roots", ["/opt/sdk", None, ["/opt/sdk", "/opt/sdk"], ["/opt/sdk", "/opt/sdk/bin"]]
)
def test_toolchain_whitelist_is_a_bounded_nonoverlapping_list(roots):
    with pytest.raises(ValueError):
        _argv(toolchain_roots=roots)


def test_explicit_toolchain_is_read_only_and_clean_environment_is_not_extended(
    monkeypatch,
):
    monkeypatch.setenv("REMOTE_TOOLCHAIN_ROOTS", "/")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/synthetic/socket")
    argv = _argv(toolchain_roots=["/opt/cross sdk", "/home/alice/toolchains"])
    assert ["--ro-bind", "/opt/cross sdk", "/opt/cross sdk"] == argv[
        argv.index("/opt/cross sdk") - 1 : argv.index("/opt/cross sdk") + 2
    ]
    assert "--bind" not in argv
    assert "SSH_AUTH_SOCK" not in argv
    assert "/synthetic/socket" not in argv
    assert (
        "/opt/cross sdk/bin:/opt/cross sdk:/home/alice/toolchains/bin:/home/alice/toolchains:/usr/bin:/bin:/usr/sbin:/sbin"
        in argv
    )


def test_mount_list_is_only_system_tools_repo_metadata_and_exact_case():
    argv = _argv(writable=True)
    mounts = [
        (arg, argv[index + 1], argv[index + 2])
        for index, arg in enumerate(argv)
        if arg in {"--bind", "--ro-bind", "--ro-bind-try"}
    ]
    assert mounts == [
        ("--ro-bind-try", root, root)
        for root in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/ld.so.cache")
    ] + [
        ("--ro-bind-try", "/srv/project/source/.repo", "/srv/project/source/.repo"),
        ("--ro-bind", "/srv/project/source/u-boot", "/srv/project/source/u-boot"),
        ("--bind", "/srv/project/worktrees/CASE-1", "/srv/project/worktrees/CASE-1"),
    ]
    assert argv[argv.index("--clearenv") + 1] == "--ro-bind-try"


def test_preflight_quotes_paths_and_checks_canonical_grants_before_mkdir(config):
    command = build_remote_command(
        _config(config),
        case_id="CASE-1",
        mode="work",
        repo_name="u-boot",
        command="printf '%s' '--bind'",
    )
    assert (
        'test "$(/usr/bin/realpath -e -- /data/home2/operator/WorkSpace/k3-ai-worktrees)"'
        in command
    )
    assert "mkdir -p" not in command
    assert command.index(
        "realpath -e -- /data/home2/operator/WorkSpace/k3-ai-worktrees)"
    ) < command.index("mkdir -m 700 --")
    assert command.endswith(shlex.quote("printf '%s' '--bind'"))


@pytest.mark.parametrize(
    "repo_paths",
    [
        ["/home/alice/.ssh"],
        ["/srv/project/source/.ssh"],
        ["/srv/project/source/u-boot", "/srv/project/source/u-boot/subrepo"],
    ],
)
def test_repository_grants_are_bounded_nonoverlapping_source_children(repo_paths):
    with pytest.raises(ValueError):
        _argv(repo_paths=repo_paths)


def test_remote_wrapper_capability_is_bound_to_exact_running_job(
    conn, config, monkeypatch
):
    raw = copy.deepcopy(_config(config).raw)
    raw["mode"] = "active"
    raw["features"]["codex"] = True
    cfg = Config(validate_config(raw), config.path)
    case_id, _ = create_case(
        conn, title="bug", case_type="bug", severity="P2", confidence=0.5
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="triage",
        expected_version=1,
    )
    transition_case(
        conn,
        case_id=case_id,
        after="investigating",
        actor_type="system",
        actor_id=None,
        reason="investigate",
        expected_version=2,
    )
    brief = "# UNTRUSTED INPUT\nx\n# FORBIDDEN ACTIONS\nNo push\n# ACCEPTANCE TESTS\nInspect"
    job_id, _ = create_codex_job(conn, cfg, case_id=case_id, brief=brief, repo="u-boot")
    workdir = conn.execute(
        "SELECT workdir FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()[0]
    capability = Path(workdir, ".capability").read_text(encoding="utf-8").strip()
    conn.execute("UPDATE jobs SET state='running',lease_owner='synthetic-worker',lease_expires_at='2099-01-01T00:00:00+00:00' WHERE job_id=?", (job_id,))
    attempt = conn.execute("SELECT attempt_no FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
    monkeypatch.setenv("K3_SUPPORT_LEASE_OWNER", "synthetic-worker")
    monkeypatch.setenv("K3_SUPPORT_EXECUTION_ROUND", str(attempt))
    monkeypatch.setenv("K3_SUPPORT_JOB_ID", job_id)
    monkeypatch.setenv("K3_SUPPORT_CASE_ID", case_id)
    monkeypatch.setenv("K3_SUPPORT_CAPABILITY", capability)
    _case_allows_work(conn, case_id)
    for field, changed in [("lease_owner", "replacement"), ("attempt_no", attempt+1),
                           ("lease_expires_at", "2000-01-01T00:00:00+00:00"),
                           ("lease_expires_at", "2099-01-01T00:00:00"), ("lease_expires_at", None)]:
        original = conn.execute(f"SELECT {field} FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        conn.execute(f"UPDATE jobs SET {field}=? WHERE job_id=?", (changed, job_id))
        with pytest.raises(RemoteSandboxError, match="lease|attempt"):
            _case_allows_work(conn, case_id)
        conn.execute(f"UPDATE jobs SET {field}=? WHERE job_id=?", (original, job_id))
    original_context = conn.execute("SELECT context_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
    for malformed in ("[]", "null", '"text"', "1"):
        conn.execute("UPDATE jobs SET context_json=? WHERE job_id=?", (malformed, job_id))
        with pytest.raises(RemoteSandboxError, match="context is invalid"):
            _case_allows_work(conn, case_id)
    conn.execute("UPDATE jobs SET context_json=? WHERE job_id=?", (original_context, job_id))
    conn.execute("UPDATE cases SET lifecycle_round=lifecycle_round+1 WHERE case_id=?", (case_id,))
    with pytest.raises(RemoteSandboxError, match="owns this Case"):
        _case_allows_work(conn, case_id)
    monkeypatch.setenv("K3_SUPPORT_JOB_ID", "job_forged")
    with pytest.raises(RemoteSandboxError, match="owns this Case"):
        _case_allows_work(conn, case_id)


def test_joint_seed_mounts_are_explicit_readonly_and_cannot_reference_current_job():
    argv = _argv(writable=True, work_id='new-job', seed_work_id='parent-a', seed_work_ids=['parent-b'])
    for name in ['parent-a', 'parent-b']:
        path = '/srv/project/worktrees/CASE-1/investigation-' + name + '/repository'
        at = argv.index(path)
        assert argv[at-1] == '--ro-bind' and argv[at+1] == path
    with pytest.raises(ValueError):
        _argv(writable=True, work_id='new-job', seed_work_ids=['new-job'])
    with pytest.raises(ValueError):
        _argv(writable=True, work_id='new-job', seed_work_ids=['../escape'])
