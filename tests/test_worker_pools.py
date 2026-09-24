from __future__ import annotations

import json
from datetime import UTC, datetime

import yaml
from test_services import _running_job

from k3_support.services import (
    _job_worker_heartbeat,
    _renew_job_health,
    job_worker_main,
)
from k3_support.store import claim_jobs, create_case
from k3_support.worker_supervisor import supervise


def test_query_and_sync_registration_does_not_supersede_long_debug(conn):
    _running_job(conn, worker_id="pool/debug/one")
    for pool in ("query", "sync"):
        assert _job_worker_heartbeat(
            conn, f"pool/{pool}/one", "ready", {}, register=True
        )
    assert (
        _renew_job_health(
            conn, job_id="job_health", worker_id="pool/debug/one", attempt_no=1
        )
        == "running"
    )
    assert not _job_worker_heartbeat(
        conn, "pool/debug/duplicate", "ready", {}, register=True
    )
    assert (
        _renew_job_health(
            conn, job_id="job_health", worker_id="pool/debug/one", attempt_no=1
        )
        == "running"
    )


def test_short_pools_claim_while_debug_remains_running(conn):
    _running_job(conn, worker_id="pool/debug/one")
    case, _ = create_case(
        conn, title="query", case_type="bug", severity="P3", confidence=0.9
    )
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (case,))
    now = datetime.now(UTC).isoformat()
    for kind in ("retrieve", "base_sync"):
        conn.execute(
            """INSERT INTO jobs(job_id,job_type,state,input_digest,case_id,
                       available_at,created_at,updated_at) VALUES(?,?,'queued',?,?,?,?,?)""",
            (kind, kind, kind, case, now, now, now),
        )
    for pool, kind in (("query", "retrieve"), ("sync", "base_sync")):
        jobs = claim_jobs(conn, f"pool/{pool}/one", job_types=(kind,))
        assert [job["job_type"] for job in jobs] == [kind]
        conn.execute(
            "UPDATE jobs SET state='succeeded',lease_owner=NULL WHERE job_id=?", (kind,)
        )
    assert (
        conn.execute("SELECT state FROM jobs WHERE job_id='job_health'").fetchone()[0]
        == "running"
    )


def test_real_worker_tick_filters_pool_before_claim(conn, config, monkeypatch):
    claims = []

    def run(_component, tick, _interval):
        tick(conn, config)

    monkeypatch.setattr("k3_support.services._run", run)
    monkeypatch.setattr(
        "k3_support.services.claim_jobs",
        lambda *args, **kwargs: claims.append(kwargs) or [],
    )
    job_worker_main("query")
    assert len(claims) == 1 and claims[0]["job_types"] == ("retrieve",)
    assert (
        conn.execute(
            "SELECT count(*) FROM service_state WHERE component='job_worker:query'"
        ).fetchone()[0]
        == 1
    )


def test_sync_worker_runs_meeting_queue_without_blocking_other_pools(
    conn, config, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        "k3_support.services._run", lambda component, tick, interval: tick(conn, config)
    )
    monkeypatch.setattr(
        "k3_support.meeting_dispatch.dispatch_one",
        lambda *args, **kwargs: calls.append(1) or {"state": "finished"},
    )
    job_worker_main("query")
    assert calls == []
    job_worker_main("sync")
    assert calls == [1]


def test_supervisor_bounds_children_and_stops_all_on_child_failure(config):
    config.path.write_text(yaml.safe_dump(config.raw))
    children = []

    class Child:
        def __init__(self, argv):
            self.pool = argv[-1]
            self.terminated = False

        def poll(self):
            return 9 if self.pool == "query" else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            return 0

    def spawn(argv, **kwargs):
        assert kwargs == {"start_new_session": True}
        child = Child(argv)
        children.append(child)
        return child

    def shutdown(processes):
        for child in processes.values():
            child.terminate()

    assert (
        supervise(config.path, popen=spawn, wait=lambda _: None, shutdown=shutdown) == 1
    )
    assert [child.pool for child in children] == ["query", "debug", "sync"]
    assert all(child.terminated for child in children if child.pool != "query")
    from k3_support.db import connect

    conn = connect(config.database_path)
    state = conn.execute(
        "SELECT detail_json FROM service_state WHERE component='job_worker'"
    ).fetchone()[0]
    assert json.loads(state)["heartbeat_phase"] == "stopped"
    assert json.loads(state)["failed_children"] == {"query": 9}
    conn.close()
