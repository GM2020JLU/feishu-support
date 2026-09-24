"""Real per-invocation PID namespace evidence, not mocked transport results."""

import json
import os
import select
import shlex
import signal
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from k3_support.remote_guard import wrap


@pytest.mark.parametrize("ending", ["normal", "eof", "silence"])
def test_journal_result_is_written_after_namespace_descendants_exit(tmp_path, ending):
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    child = "import os,time\nassert not os.path.exists(" + repr(str(receipts)) + "), 'receipt directory exposed'\n" + """
pid=os.fork()
if pid==0:
    os.setsid()
    print('READY',flush=True)
    time.sleep(60)
else:
    while not os.path.exists('/evidence/finish'):
        time.sleep(.01)
    raise SystemExit(7)
"""
    argv = ["/usr/bin/bwrap", "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid",
            "--unshare-ipc", "--unshare-net", "--clearenv"]
    for root in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        argv.extend(["--ro-bind-try", root, root])
    argv.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                 "--bind", str(evidence), "/evidence", "--", "/usr/bin/python3", "-I", "-S", "-c", child])
    request = str(uuid4())
    process = subprocess.Popen(shlex.split(wrap("exec " + shlex.join(argv),
                               receipt_directory=str(receipts), request_id=request)),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    fds = []
    try:
        process.stdin.write((str(int(time.time()*1000)+5000)+"\n").encode())
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 4)[0], "sandbox readiness timed out"
        assert process.stdout.readline() == b"READY\n"
        root = process.pid
        todo = list(map(int, Path(f"/proc/{root}/task/{root}/children").read_text().split()))
        pids = []
        while todo:
            pid = todo.pop()
            if pid in pids:
                continue
            assert len(pids) < 16
            fds.append(os.pidfd_open(pid))
            pids.append(pid)
            todo.extend(map(int, Path(f"/proc/{pid}/task/{pid}/children").read_text().split()))
        assert len(pids) >= 4 and len({os.getsid(pid) for pid in pids}) >= 2
        if ending == "normal":
            (evidence / "finish").touch()
        elif ending == "eof":
            process.stdin.close()
        result_path = receipts / (request + ".result")
        deadline = time.monotonic() + 8
        result = None
        while time.monotonic() < deadline:
            try:
                result = json.loads(result_path.read_text())
                break
            except (FileNotFoundError, ValueError):
                time.sleep(.01)
        assert result is not None, "guardian produced no durable result"
        # This assertion precedes all fallback termination. pidfds prevent PID
        # reuse from turning an unrelated exit into apparent cleanup evidence.
        assert set(select.select(fds, [], [], 0)[0]) == set(fds)
        assert result["guard_exit_code"] == (7 if ending == "normal" else 124)
        assert process.wait(timeout=3) == result["guard_exit_code"]
    finally:
        for fd in reversed(fds):
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(fd)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
