import os
import sys

import pytest

from k3_support.broker_process import run_process


def args(tmp_path, code):
    return {"argv": [sys.executable, "-c", code], "cwd": tmp_path,
            "env": {"HOME": str(tmp_path)}, "stdin": b"test input",
            "timeout": 2, "heartbeat_interval": 0.02}


def test_real_child_stdin_output_and_clean_environment(tmp_path):
    result = run_process(**args(tmp_path, "import os,sys; assert 'PATH' not in os.environ; print(sys.stdin.read())"),
                         heartbeat=lambda: None)
    assert result == "test input\n"


def test_heartbeat_failure_kills_and_reaps_real_child(tmp_path):
    calls = []
    marker = tmp_path / "pid"
    def heartbeat():
        calls.append(True)
        if marker.exists():
            raise ValueError("synthetic revoked")
    code = "import os,pathlib,time; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(30)"
    with pytest.raises(ValueError, match="revoked"):
        run_process(**args(tmp_path, code), heartbeat=heartbeat)
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert len(calls) >= 2


def test_output_flood_is_bounded(tmp_path):
    with pytest.raises(ValueError, match="output limit"):
        run_process(**args(tmp_path, "import sys; sys.stdout.write('x'*1000000)"),
                    heartbeat=lambda: None, output_limit=1024)


def test_nonzero_exit_is_not_success(tmp_path):
    with pytest.raises(ValueError, match="command failed"):
        run_process(**args(tmp_path, "raise SystemExit(2)"), heartbeat=lambda: None)


def test_execution_timeout_reaps_child(tmp_path):
    options = args(tmp_path, "import os,pathlib,time; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(30)")
    options["timeout"] = 0.1
    with pytest.raises(TimeoutError):
        run_process(**options, heartbeat=lambda: None)
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "pid").read_text()), 0)


def test_stderr_also_counts_toward_output_limit(tmp_path):
    with pytest.raises(ValueError, match="output limit"):
        run_process(**args(tmp_path, "import sys; sys.stderr.write('x'*1000000)"),
                    heartbeat=lambda: None, output_limit=1024)
