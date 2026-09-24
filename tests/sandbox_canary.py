"""Guarded, synthetic-only child-process canary for the real local bubblewrap.

Run only through test_sandbox_canary.py.  No production config, home, transport,
or socket is opened.  This is a mount/namespace canary, not broker-identity proof.
"""

from __future__ import annotations

import json
import os
import select
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    fixture = Path(sys.argv[1]).resolve(strict=True)
    virtual = sys.argv[2]
    assert fixture.name == "sandbox-canary"
    assert (fixture / "SYNTHETIC_ONLY").read_text() == "no production resources\n"
    assert virtual in {"/srv/synthetic-project", "/home/synthetic-user/project"}
    tool = Path("/usr/bin/bwrap")
    if not tool.is_file():
        print(
            json.dumps({"status": "unavailable", "reason": "bubblewrap not installed"})
        )
        return 0
    info = tool.stat()
    assert info.st_uid == 0 and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from k3_support.remote_sandbox import (
        SYSTEM_TOOLCHAIN_ROOTS,
        render_remote_command,
        sandbox_argv,
    )

    hostroot = fixture / "hostroot"
    hostroot.mkdir()

    def local(path: str) -> Path:
        assert path.startswith("/") and ".." not in Path(path).parts
        result = hostroot / path.lstrip("/")
        assert result.resolve().is_relative_to(hostroot)
        return result

    source = virtual + "/source"
    worktrees = virtual + "/worktrees"
    repo = source + "/u-boot"
    case = worktrees + "/CASE-1"
    sdk = "/opt/synthetic-sdk"
    for path in (repo, case, sdk + "/bin", "/home/other-user/.ssh", "/run/user/4242"):
        local(path).mkdir(parents=True, exist_ok=True)
    local("/home/other-user/.ssh/sentinel").write_text("SYNTHETIC_PRIVATE_SENTINEL")
    local(worktrees + "/OTHER-CASE").mkdir()
    local(worktrees + "/OTHER-CASE/sentinel").write_text("OTHER_CASE")
    local(sdk + "/bin/fixture-compiler").write_text(
        "#!/bin/sh\nprintf 'fixture-compiler-ready\\n'\n"
    )
    local(sdk + "/bin/fixture-compiler").chmod(0o755)
    clean = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(fixture / "empty-home"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C.UTF-8",
        "CANARY_HOST_ONLY": "SYNTHETIC_ENV",
        "SSH_AUTH_SOCK": str(local("/run/user/4242/control.sock")),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path="
        + str(local("/run/user/4242/control.sock")),
        "BASH_ENV": str(fixture / "absent-bash-env"),
    }
    Path(clean["HOME"]).mkdir()

    # Audit every launch in this child: only fixed local Git setup and the exact
    # generated sandbox command below.  Never use the inherited developer env.
    approved_shells: set[str] = set()
    original_popen = subprocess.Popen

    def guarded_popen(argv, *args, **kwargs):
        assert not kwargs.get("shell") and isinstance(argv, list)
        assert kwargs.get("env") == clean
        if argv[0] == "/usr/bin/git":
            assert argv[1] in {"init", "config", "add", "commit", "rev-parse"}
            assert Path(kwargs["cwd"]).resolve().is_relative_to(hostroot)
        elif argv[0] == "/bin/bash":
            assert argv[:4] == ["/bin/bash", "--noprofile", "--norc", "-c"]
            assert argv[4] in approved_shells and kwargs["cwd"] == fixture
        else:
            raise AssertionError("unapproved canary process")
        return original_popen(argv, *args, **kwargs)

    subprocess.Popen = guarded_popen

    def git(*args: str) -> str:
        result = subprocess.run(
            ["/usr/bin/git", *args],
            cwd=local(repo),
            env=clean,
            text=True,
            capture_output=True,
            check=True,
            timeout=15,
        )
        return result.stdout.strip()

    git("init", "--initial-branch=main")
    git("config", "user.name", "Synthetic Canary")
    git("config", "user.email", "synthetic-canary@example.invalid")
    local(repo + "/sample.c").write_text(
        "/* synthetic fixture */\nint main(void) { return 0; }\n"
    )
    git("add", "sample.c")
    git("commit", "-m", "synthetic fixture only")
    commit = git("rev-parse", "HEAD")
    # Model Android-repo's shared object storage: the repository has no objects
    # of its own.  Its absolute alternate resolves through the read-only .repo.
    shared = source + "/.repo/project-objects/u-boot.git/objects"
    local(shared).parent.mkdir(parents=True)
    local(repo + "/.git/objects").rename(local(shared))
    local(repo + "/.git/objects/info").mkdir(parents=True)
    local(repo + "/.git/objects/info/alternates").write_text(shared + "\n")
    metadata_git = source + "/.repo/projects/u-boot.git"
    local(metadata_git).parent.mkdir(parents=True)
    local(repo + "/.git").rename(local(metadata_git))
    local(repo + "/.git").symlink_to(metadata_git, target_is_directory=True)
    # An in-repo symlink still cannot resolve outside the whitelist.
    local(repo + "/private-link").symlink_to("/home/other-user/.ssh/sentinel")
    namespaces = {
        name: os.readlink("/proc/self/ns/" + name) for name in ("pid", "ipc", "net")
    }
    parent_pid = os.getpid()
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(1)
    port = tcp.getsockname()[1]
    unix = socket.socket(socket.AF_UNIX)
    # AF_UNIX has a short sockaddr path limit; bind relative to the strictly
    # synthetic directory rather than shortening via a real host /run socket.
    os.chdir(local("/run/user/4242"))
    unix.bind("control.sock")
    os.chdir(fixture)
    unix.listen(1)
    local_probe = """
import json, os, pathlib, socket, sys
data=json.loads(sys.argv[1])
for name, expected in data['namespaces'].items():
    assert os.readlink('/proc/self/ns/'+name) != expected, name
if data['parent_pid'] > 20:
    assert not pathlib.Path('/proc/'+str(data['parent_pid'])).exists()
assert os.environ['HOME'] == '/tmp/home'
assert not any(key in os.environ for key in ('CANARY_HOST_ONLY','SSH_AUTH_SOCK','DBUS_SESSION_BUS_ADDRESS','BASH_ENV'))
assert not pathlib.Path('/run').exists()
assert not pathlib.Path('/home/other-user/.ssh/sentinel').exists()
assert not pathlib.Path(data['physical_sentinel']).exists()
assert not pathlib.Path(data['repo']+'/private-link').exists()
assert not pathlib.Path(data['other_case']).exists()
for family, address in ((socket.AF_UNIX, '/run/user/4242/control.sock'), (socket.AF_INET, ('127.0.0.1',data['port']))):
    sock=socket.socket(family); sock.settimeout(0.2)
    try:
        sock.connect(address)
    except OSError:
        pass
    else:
        raise AssertionError('synthetic host listener reachable')
    finally:
        sock.close()
for root in (data['repo'], data['metadata'], data['sdk']):
    try:
        pathlib.Path(root+'/should-not-write').write_text('forbidden')
    except OSError:
        pass
    else:
        raise AssertionError('readonly grant writable: '+root)
print('namespace-and-mount-canary-ok')
"""
    probe_data = {
        "namespaces": namespaces,
        "parent_pid": parent_pid,
        "port": port,
        "repo": repo,
        "metadata": source + "/.repo",
        "sdk": sdk,
        "physical_sentinel": str(local("/home/other-user/.ssh/sentinel")),
        "other_case": worktrees + "/OTHER-CASE",
    }
    payload = " && ".join(
        [
            shlex.join(["python3", "-I", "-c", local_probe, json.dumps(probe_data)]),
            "fixture-compiler",
            shlex.join(["git", "-C", repo, "cat-file", "-e", commit + "^{commit}"]),
            shlex.join(
                ["git", "clone", "--shared", "--no-checkout", repo, case + "/clone"]
            ),
            shlex.join(["git", "-C", case + "/clone", "checkout", commit]),
            shlex.join(
                ["cc", case + "/clone/sample.c", "-o", case + "/synthetic-binary"]
            ),
            shlex.quote(case + "/synthetic-binary"),
        ]
    )
    argv = sandbox_argv(
        case_id="CASE-1",
        source_root=source,
        worktree_root=worktrees,
        repo_paths=[repo],
        toolchain_roots=[sdk],
        writable=True,
        command=payload,
    )
    argv[0] = "/usr/bin/bwrap"
    allowed_grants = {source + "/.repo", repo, case, sdk}
    for index, arg in enumerate(argv[: argv.index("--")]):
        if arg in {"--bind", "--ro-bind", "--ro-bind-try"}:
            src, dst = argv[index + 1 : index + 3]
            if src in (*SYSTEM_TOOLCHAIN_ROOTS, "/etc/ld.so.cache"):
                assert src == dst and arg == "--ro-bind-try"
            else:
                assert src == dst and src in allowed_grants
                fixture_src = local(src)
                assert fixture_src.is_dir() and not fixture_src.is_symlink()
                argv[index + 1] = str(fixture_src)
            assert arg != "--bind" or dst == case
    assert all(
        flag in argv
        for flag in (
            "--clearenv",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-net",
            "--new-session",
        )
    )
    # Only mount sources are replaced with guarded synthetic directories.  The
    # production flags and /srv or /home destination policy remain unchanged.
    command = render_remote_command(argv, writable=True)
    approved_shells.add(command)
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        cwd=fixture,
        env=clean,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        denied = (
            "No permissions to create a new namespace",
            "Creating new namespace failed: Operation not permitted",
        )
        if any(reason in result.stderr for reason in denied):
            print(
                json.dumps(
                    {
                        "status": "unavailable",
                        "reason": "kernel denied required namespaces; no weakened fallback",
                    }
                )
            )
            return 0
        print(
            json.dumps(
                {"status": "failed", "stdout": result.stdout, "stderr": result.stderr}
            )
        )
        return 1
    assert local(case + "/clone/sample.c").is_file()
    assert local(case + "/synthetic-binary").is_file()
    assert not local(repo + "/should-not-write").exists()
    assert "namespace-and-mount-canary-ok" in result.stdout
    assert "fixture-compiler-ready" in result.stdout
    # Reuse the exact mount list without the one writable grant to execute a
    # real inspection.  An existing host Case must not appear just because its
    # synthetic parent directory exists in the sandbox.
    inspect_argv = list(argv)
    bind_index = inspect_argv.index("--bind")
    del inspect_argv[bind_index : bind_index + 3]
    inspect_argv[inspect_argv.index("--chdir") + 1] = source
    inspect_argv[-1] = " && ".join(
        [
            shlex.join(["python3", "-I", "-c", local_probe, json.dumps(probe_data)]),
            shlex.join(["git", "-C", repo, "cat-file", "-e", commit + "^{commit}"]),
            shlex.join(["test", "!", "-e", case]),
        ]
    )
    inspect_command = render_remote_command(inspect_argv, writable=False)
    approved_shells.add(inspect_command)
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", inspect_command],
        cwd=fixture,
        env=clean,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Actual namespace teardown, including a descendant which starts a session
    # of its own. This is local bwrap evidence, not an SSH disconnect guarantee.
    stop_argv = list(argv)
    stop_argv[-1] = shlex.join(["python3", "-I", "-c",
        ("import os,pathlib,time; child=os.fork(); "
        "os.setsid() if child==0 else None; "
        f"pathlib.Path({case + '/stop-ready'!r}).touch() if child==0 else None; time.sleep(30)")])
    stop_command = render_remote_command(stop_argv, writable=True)
    approved_shells.add(stop_command)
    process = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-c", stop_command],
                               cwd=fixture, env=clean, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    descriptors = []
    try:
        deadline = time.monotonic() + 5
        while not local(case + "/stop-ready").exists():
            assert time.monotonic() < deadline, "sandbox descendant did not start"
            time.sleep(0.02)
        pending, owned = [process.pid], []
        while pending:
            pid = pending.pop()
            owned.append(pid)
            descriptors.append(os.pidfd_open(pid))
            pending.extend(int(value) for value in Path(f"/proc/{pid}/task/{pid}/children").read_text().split())
        assert len(owned) >= 3
        assert sum(os.getsid(pid) == pid for pid in owned) >= 2
        process.kill()
        process.wait(timeout=5)
        remaining = list(descriptors)
        deadline = time.monotonic() + 5
        while remaining:
            assert time.monotonic() < deadline, "sandbox descendants did not exit"
            ready, _, _ = select.select(remaining, [], [], 0.1)
            remaining = [fd for fd in remaining if fd not in ready]
    finally:
        for fd in descriptors:
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(fd)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
    # The preflight refuses a top-level symlink before bwrap runs.  This is not
    # claimed to close a same-UID path-swap race between preflight and mount.
    local(repo).rename(local(source + "/actual-repo"))
    local(repo).symlink_to(local(source + "/actual-repo"), target_is_directory=True)
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        cwd=fixture,
        env=clean,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert (
        result.returncode != 0 and "namespace-and-mount-canary-ok" not in result.stdout
    )
    tcp.close()
    unix.close()
    print(
        json.dumps(
            {
                "status": "passed",
                "virtual_root": virtual,
                "checks": [
                    "minimal_root",
                    "pid_ipc_net",
                    "synthetic_home_socket_hidden",
                    "readonly_sources",
                    "inspect_has_no_case_mount",
                    "exact_case_write",
                    "shared_repo_objects",
                    "local_clone_and_build",
                    "symlink_grant_denied",
                    "namespace_descendants_exit_including_setsid",
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
