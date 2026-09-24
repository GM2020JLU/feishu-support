import os
import select
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_coding_budget import setup

from k3_support import executors
from k3_support.executors import ExecutorError, _monitor_codex_process


@pytest.mark.parametrize("mutation", [
    "attempt_no=attempt_no+1", "lease_owner='replacement'", "lifecycle_round=lifecycle_round+1",
    "state='cancelled'",
])
def test_old_process_cannot_overwrite_replacement_identity(conn, config, monkeypatch, mutation):
    _, job_id = setup(conn, config)
    conn.execute("UPDATE jobs SET state='running',lease_owner='original',pid=111,process_start_token='first'")
    job = dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
    conn.execute(f"UPDATE jobs SET {mutation},pid=222,process_start_token='replacement'")
    before = list(conn.iterdump())
    monkeypatch.setattr("k3_support.executors._process_start_token", lambda _: "old-spawn")
    with pytest.raises(ExecutorError, match="identity changed"):
        _monitor_codex_process(conn, job_id=job_id, job=job, argv=["synthetic"],
                               process=SimpleNamespace(pid=333), timeout=5,
                               execution_permitted=lambda: True)
    assert list(conn.iterdump()) == before


def test_real_spawn_race_reaps_old_child_without_touching_replacement(conn, config, monkeypatch):
    _, job_id = setup(conn, config)
    conn.execute("UPDATE jobs SET state='running',lease_owner='original'")
    workdir = conn.execute("SELECT workdir FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
    original = subprocess.Popen
    children = []
    descriptors = []

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        descriptors.append(os.pidfd_open(process.pid))
        # Force replacement precisely between Popen and PID registration.
        conn.execute("UPDATE jobs SET attempt_no=attempt_no+1,lease_owner='replacement',pid=222,process_start_token='replacement'")
        return process

    monkeypatch.setattr(executors.subprocess, "Popen", spawn)
    try:
        with pytest.raises(ExecutorError, match="identity changed"):
            executors._run_codex_supervised(
                conn, job_id=job_id,
                argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=workdir, timeout=5, should_stop=lambda: False, codex_home=Path(workdir))
        assert len(children) == 1 and children[0].returncode is not None
        assert select.select(descriptors, [], [], 0)[0] == descriptors
        row = conn.execute("SELECT lease_owner,pid,process_start_token FROM jobs").fetchone()
        assert tuple(row) == ("replacement", 222, "replacement")
        assert conn.execute("SELECT count(*) FROM execution_exit_receipts").fetchone()[0] == 0
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        for descriptor in descriptors:
            os.close(descriptor)
