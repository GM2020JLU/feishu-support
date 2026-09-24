import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_coding_budget import setup

from k3_support import executors


@pytest.mark.parametrize("name", ["python", "worker name", "worker ) ( task", "worker\nname)"])
def test_process_start_token_handles_parenthesized_names(monkeypatch, name):
    fields = ["S"] + ["0"] * 18 + ["123456789"] + ["0"] * 5
    raw = f"123 ({name}) " + " ".join(fields)
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: raw)
    assert executors._process_start_token(123) == "123456789"


@pytest.mark.parametrize("raw", ["", "123 (broken", "999 (wrong) " + "0 " * 30, "123 (short) S", "123 (x) " + "0 " * 19 + "invalid"])
def test_process_start_token_malformed_is_unknown(monkeypatch, raw):
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: raw)
    assert executors._process_start_token(123) == "unavailable"


@pytest.mark.parametrize("pid", [True, -1, 0, "123"])
def test_invalid_pid_never_reads_proc(monkeypatch, pid):
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: pytest.fail("invalid PID"))
    assert executors._process_start_token(pid) == "unavailable"


def test_real_owned_process_can_be_stopped_and_reaped():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        executors._stop_process_group(process)
        out, err = executors._drain_stopped_process(process)
        assert process.returncode is not None
        assert out == "" and err == ""
        assert process.stdout.closed and process.stderr.closed
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            stream.close()


@pytest.mark.parametrize("phase", ["term", "kill"])
def test_group_disappears_during_stop_still_reaps(monkeypatch, phase):
    waits, signals = [], []

    class Process:
        pid = 123456

        def poll(self):
            return None

        def wait(self, timeout):
            waits.append(timeout)
            if phase == "kill" and timeout == 15:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return 0

    def killpg(pid, sig):
        assert pid == 123456
        signals.append(sig)
        if phase == "term" or len(signals) == 2:
            raise ProcessLookupError()

    monkeypatch.setattr(executors.os, "killpg", killpg)
    executors._stop_process_group(Process())
    assert waits == ([5] if phase == "term" else [15, 5])


def test_group_permission_error_is_not_treated_as_exit(monkeypatch):
    class Process:
        pid = 123456

        def poll(self):
            return None

        def wait(self, timeout):
            pytest.fail("permission failure is not proof of exit")

    def denied(*args):
        raise PermissionError("denied")

    monkeypatch.setattr(executors.os, "killpg", denied)
    with pytest.raises(PermissionError):
        executors._stop_process_group(Process())


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE jobs SET state='cancelled'",
        "UPDATE jobs SET lease_owner='replacement'",
        "UPDATE jobs SET attempt_no=attempt_no+1",
        "UPDATE cases SET lifecycle_round=lifecycle_round+1",
        "UPDATE cases SET state='paused'",
    ],
)
def test_authority_change_stops_live_process_group(conn, config, monkeypatch, mutation):
    _, job = setup(conn, config)
    conn.execute(
        "UPDATE jobs SET state='running',lease_owner='worker',attempt_no=1 WHERE job_id=?",
        (job,),
    )
    workdir = conn.execute(
        "SELECT workdir FROM jobs WHERE job_id=?", (job,)
    ).fetchone()[0]
    stopped = []

    class Process:
        pid = 123456
        returncode = -15

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            if timeout == 5:
                return "", ""
            assert timeout <= 2
            conn.execute(mutation)
            raise subprocess.TimeoutExpired("fake", timeout)

    process = Process()
    monkeypatch.setattr(executors.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(executors, "_process_start_token", lambda pid: "fixture")
    monkeypatch.setattr(executors, "_stop_process_group", lambda p: stopped.append(p))
    result = executors._run_codex_supervised(
        conn,
        job_id=job,
        argv=["fake"],
        cwd=workdir,
        timeout=60,
        should_stop=lambda: False,
        codex_home=Path(workdir),
    )
    assert result.returncode == 143 and stopped == [process]
    receipt = conn.execute(
        "SELECT attempt_no,pid,process_start_token,returncode FROM execution_exit_receipts"
    ).fetchone()
    assert tuple(receipt) == (1, 123456, "fixture", -15)


def test_stopped_process_drain_is_bounded_and_preserves_partial_output():
    import io

    class Process:
        stdout = io.StringIO()
        stderr = io.StringIO()

        def communicate(self, timeout=None):
            assert timeout == 5
            raise subprocess.TimeoutExpired("fixture", timeout, output=b"partial\xff", stderr=b"diagnostic")

    process = Process()
    out, err = executors._drain_stopped_process(process)
    assert out == "partial\ufffd"
    assert "diagnostic" in err and "descendant exit is unverified" in err
    assert process.stdout.closed and process.stderr.closed


def test_failed_stop_check_prevents_start(conn, config, monkeypatch):
    _, job = setup(conn, config)
    conn.execute("UPDATE jobs SET state='running' WHERE job_id=?", (job,))
    workdir = conn.execute(
        "SELECT workdir FROM jobs WHERE job_id=?", (job,)
    ).fetchone()[0]

    def fail():
        raise RuntimeError("authority unavailable")

    monkeypatch.setattr(
        executors.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not start")
    )
    with pytest.raises(executors.ExecutorError, match="authority changed"):
        executors._run_codex_supervised(
            conn,
            job_id=job,
            argv=["fake"],
            cwd=workdir,
            timeout=60,
            should_stop=fail,
            codex_home=Path(workdir),
        )


@pytest.mark.parametrize("failure", ["pid_write", "monitor"])
def test_database_write_failure_after_start_stops_and_reaps_child(
    conn, config, monkeypatch, failure
):
    _, job = setup(conn, config)
    conn.execute("UPDATE jobs SET state='running' WHERE job_id=?", (job,))
    workdir = conn.execute(
        "SELECT workdir FROM jobs WHERE job_id=?", (job,)
    ).fetchone()[0]
    if failure == "pid_write":
        conn.execute(
            "CREATE TEMP TRIGGER fail_pid BEFORE UPDATE OF pid ON jobs BEGIN SELECT RAISE(ABORT,'write failed'); END"
        )
    calls = []

    class Process:
        pid = 123456
        returncode = None
        stdout = stderr = None

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            if failure == "monitor" and timeout <= 2:
                raise sqlite3.IntegrityError("write failed")
            calls.append(("reap", timeout))
            return "", ""

    process = Process()

    def stop(p):
        calls.append(("stop", p.pid))
        p.returncode = -15

    monkeypatch.setattr(executors.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(executors, "_process_start_token", lambda pid: "fixture")
    monkeypatch.setattr(executors, "_stop_process_group", stop)
    with pytest.raises(sqlite3.IntegrityError, match="write failed"):
        executors._run_codex_supervised(
            conn,
            job_id=job,
            argv=["fake"],
            cwd=workdir,
            timeout=60,
            should_stop=lambda: False,
            codex_home=Path(workdir),
        )
    assert calls == [("stop", 123456), ("reap", 5)]
    assert (
        conn.execute("SELECT count(*) FROM execution_exit_receipts").fetchone()[0] == 0
    )
