"""Real local processes and SQLite only; no board, model or transport calls."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from k3_support.store import create_case
from k3_support.worker_supervisor import stop_process_groups


def ready(process):
    assert select.select([process.stdout], [], [], 5)[0], "child did not become ready"
    return process.stdout.readline().strip()


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def test_exited_leader_does_not_leave_term_ignoring_descendant(tmp_path):
    child = """import subprocess, sys
code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
p = subprocess.Popen([sys.executable,'-I','-c',code],stdout=subprocess.PIPE,text=True)
assert p.stdout.readline().strip() == 'ready'
print(p.pid,flush=True)
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", child],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    try:
        descendant = int(ready(process))
        assert process.wait(timeout=5) == 0
        assert os.getpgid(descendant) == process.pid
        stop_process_groups({"debug": process}, grace_seconds=0.05)
        state = Path(f"/proc/{descendant}/stat")
        # A zombie awaiting init's reap cannot execute code or retain a lease.
        import time

        deadline = time.monotonic() + 2
        while state.exists() and state.read_text().split(") ", 1)[1][0] != "Z":
            assert time.monotonic() < deadline, "descendant still executing"
            time.sleep(0.01)
    finally:
        stop(process)


def test_three_processes_short_claims_complete_while_debug_waits(
    conn, config, tmp_path
):
    case, _ = create_case(
        conn, title="pool barrier", case_type="bug", severity="P3", confidence=0.9
    )
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (case,))
    now = datetime.now(UTC).isoformat()
    for kind in ("codex", "retrieve", "base_sync"):
        conn.execute(
            """INSERT INTO jobs(job_id,job_type,state,input_digest,case_id,
                       available_at,created_at,updated_at) VALUES(?,?,'queued',?,?,?,?,?)""",
            (kind, kind, kind, case, now, now, now),
        )
    source = str(Path(__file__).resolve().parents[1] / "src")
    script = """import sys,time
sys.path.insert(0,sys.argv[1])
from k3_support.db import connect
from k3_support.store import claim_jobs
def guard(event,args):
    if event.startswith('socket.') or event in {'subprocess.Popen','os.system'}:
        raise RuntimeError('fixture transport forbidden')
sys.addaudithook(guard)
c=connect(sys.argv[2]); kind=sys.argv[3]
assert len(claim_jobs(c,'fixture-'+kind,job_types=(kind,))) == 1
if kind == 'codex':
    print('claimed',flush=True)
    time.sleep(60)
else:
    c.execute("UPDATE jobs SET state='succeeded',lease_owner=NULL WHERE job_id=?",(kind,))
    print('done',flush=True)
"""
    processes = []
    try:
        for kind in ("codex", "retrieve", "base_sync"):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    script,
                    source,
                    str(config.database_path),
                    kind,
                ],
                cwd=tmp_path,
                stdout=subprocess.PIPE,
                text=True,
                env={"PATH": "/usr/bin:/bin"},
                start_new_session=True,
            )
            processes.append(process)
            assert ready(process) == ("claimed" if kind == "codex" else "done")
            if kind != "codex":
                assert process.wait(timeout=5) == 0
        assert processes[0].poll() is None
        assert dict(conn.execute("SELECT job_type,state FROM jobs")) == {
            "codex": "running",
            "retrieve": "succeeded",
            "base_sync": "succeeded",
        }
    finally:
        for process in processes:
            stop(process)


def test_real_supervisor_entry_starts_three_workers_and_stops(conn, config, tmp_path):
    import time

    import yaml

    config.path.write_text(yaml.safe_dump(config.raw))
    source = str(Path(__file__).resolve().parents[1] / "src")
    output = tmp_path / "supervisor.log"
    pids = set()
    with output.open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "k3_support.worker_supervisor",
                "--config",
                str(config.path),
            ],
            cwd=tmp_path,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": source,
                "HOME": str(tmp_path),
                "XDG_CONFIG_HOME": str(tmp_path / "empty-config"),
            },
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                rows = conn.execute(
                    "SELECT component,pid,status FROM service_state WHERE component LIKE 'job_worker:%'"
                ).fetchall()
                pids.update(row["pid"] for row in rows)
                if len(rows) == 3 and all(row["status"] == "ready" for row in rows):
                    break
                assert process.poll() is None, output.read_text()
                assert time.monotonic() < deadline, output.read_text()
                time.sleep(0.02)
            assert len(pids) == 3
            assert all(os.getpgid(pid) == pid for pid in pids)
            from k3_support.worker_supervisor import supervise

            with pytest.raises(RuntimeError, match="already running"):
                supervise(
                    config.path,
                    popen=lambda *a, **k: pytest.fail("duplicate spawned child"),
                )
            process.terminate()
            assert process.wait(timeout=10) == 0, output.read_text()
            assert (
                conn.execute(
                    "SELECT status FROM service_state WHERE component='job_worker'"
                ).fetchone()[0]
                == "stopped"
            )
            for pid in pids:
                assert not Path(f"/proc/{pid}").exists()
            from k3_support.services import _job_worker_heartbeat

            for pool in ("query", "debug", "sync"):
                assert _job_worker_heartbeat(
                    conn, f"pool/{pool}/fresh-restart", "ready", {}, register=True
                )
            assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        finally:
            for pid in pids:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            stop(process)
