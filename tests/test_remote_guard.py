import os
import shlex
import signal
import subprocess
import sys
import time

import pytest

from k3_support.broker_process import run_process
from k3_support.remote_guard import wrap


def test_guard_roundtrip_uses_heartbeats_and_preserves_output(tmp_path):
    result = run_process(argv=shlex.split(wrap("printf synthetic; printf diagnostic >&2; exit 7")),
                         cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"}, stdin=b"", heartbeat=lambda: None,
                         timeout=10, heartbeat_interval=.1, detailed=True, keepalive=True)
    assert result == {"exit_code": 7, "stdout": "synthetic", "stderr": "diagnostic"}


@pytest.mark.parametrize("end", ["eof", "stale", "signal", "silent"])
def test_guard_stops_real_child_on_lost_lease(tmp_path, end):
    marker = tmp_path / "child.pid"
    code = "import os,time; open("+repr(str(marker))+",'w').write(str(os.getpid())); time.sleep(60)"
    command = "exec " + shlex.join([sys.executable, "-c", code])
    process = subprocess.Popen(shlex.split(wrap(command)), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    fd = None
    try:
        process.stdin.write((str(int(time.time()*1000)+5000)+"\n").encode()); process.stdin.flush()
        deadline = time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline:
            time.sleep(.01)
        assert marker.exists()
        fd = os.pidfd_open(int(marker.read_text()))
        if end == "eof":
            process.stdin.close()
        elif end == "stale":
            process.stdin.write(b"1000000000000\n"); process.stdin.flush()
        elif end == "signal":
            process.send_signal(signal.SIGHUP)
        assert process.wait(timeout=7) == 124
        import select
        assert select.select([fd], [], [], 2)[0] == [fd]
    finally:
        if fd is not None:
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


def test_guard_rejects_expired_start_before_command(tmp_path):
    marker = tmp_path / "must-not-exist"
    result = subprocess.run(shlex.split(wrap("touch " + shlex.quote(str(marker)))),
                            input=b"1000000000000\n", capture_output=True, timeout=7, check=False)
    assert result.returncode == 124 and not marker.exists()
