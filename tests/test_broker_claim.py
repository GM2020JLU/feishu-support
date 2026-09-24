import json
import os
from datetime import UTC, datetime

import pytest
from test_attention_races import race
from test_broker_task_binding import seed
from test_review import active_config

from k3_support.broker_claim import claim_next
from k3_support.broker_grants import verify_bound_task
from k3_support.db import transaction
from k3_support.ids import canonical_json, digest
from k3_support.workbench import workbench_snapshot

NOW = datetime(2026, 9, 8, tzinfo=UTC)
UID = os.geteuid() + 1


def queued(conn):
    params = seed(conn)
    payload = {"case_id": params["case_id"], "lifecycle_round": 1, "brief": "synthetic",
               "repos": ["u-boot"], "model": "gpt-5.6-sol", "reasoning": "medium", "context_extra": {}}
    conn.execute("INSERT INTO broker_inputs VALUES(?,?,?)", ("job-1", canonical_json(payload), "now"))
    conn.execute("UPDATE jobs SET input_digest=?", (digest(payload),))
    conn.execute("UPDATE jobs SET state='queued',attempt_no=0,available_at='2020-01-01T00:00:00+00:00'")


def test_atomic_assignment_can_authenticate(conn, config):
    queued(conn)
    cfg = active_config(config)
    result = claim_next(conn, cfg, worker_uid=UID, now=NOW)
    assert result is not None
    with transaction(conn):
        assert verify_bound_task(conn, result, peer_uid=UID, now=NOW)["attempt_no"] == 1
    assert result["lease_token"] not in "\n".join(conn.iterdump())
    assert claim_next(conn, cfg, worker_uid=UID, now=NOW) is None


def test_grant_failure_rolls_back_job_assignment(conn, config, monkeypatch):
    queued(conn)
    def fail(*args, **kwargs):
        raise ValueError("synthetic grant failure")
    monkeypatch.setattr("k3_support.broker_claim.issue", fail)
    with pytest.raises(ValueError):
        claim_next(conn, active_config(config), worker_uid=UID, now=NOW)
    row = conn.execute("SELECT state,attempt_no FROM jobs").fetchone()
    assert tuple(row) == ("queued", 0)
    assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 0


def test_concurrent_control_assignment_has_one_winner(conn, config):
    queued(conn)
    cfg = active_config(config)
    def claim(db):
        return claim_next(db, cfg, worker_uid=UID, now=NOW)
    results = race(config, claim, claim)
    assert sum(result is not None for result in results) == 1
    assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 1


def test_shadow_and_stale_lifecycle_not_assigned(conn, config):
    queued(conn)
    assert claim_next(conn, config, worker_uid=UID, now=NOW) is None
    conn.execute("UPDATE cases SET lifecycle_round=2")
    assert claim_next(conn, active_config(config), worker_uid=UID, now=NOW) is None
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "queued"


def test_legacy_job_without_snapshot_stays_queued(conn, config):
    queued(conn)
    conn.execute("DELETE FROM broker_inputs")
    assert claim_next(conn, active_config(config), worker_uid=UID, now=NOW) is None
    assert tuple(conn.execute("SELECT state,attempt_no FROM jobs").fetchone()) == ("queued", 0)


@pytest.mark.parametrize("bad", ["digest", "oversized", "schema"])
def test_invalid_input_never_consumes_an_attempt(conn, config, bad):
    queued(conn)
    payload = json.loads(conn.execute("SELECT payload_json FROM broker_inputs").fetchone()[0])
    if bad == "schema":
        del payload["brief"]
    else:
        payload["brief"] = "文" * 50000 if bad == "oversized" else "changed"
    conn.execute("UPDATE broker_inputs SET payload_json=?", (canonical_json(payload),))
    if bad != "digest":
        conn.execute("UPDATE jobs SET input_digest=?", (digest(payload),))
    assert claim_next(conn, active_config(config), worker_uid=UID, now=NOW) is None
    assert tuple(conn.execute("SELECT state,attempt_no FROM jobs").fetchone()) == ("waiting", 0)
    assert conn.execute("SELECT error_class FROM jobs").fetchone()[0] == "broker_input_invalid"
    assert workbench_snapshot(conn, config=config)["counts"]["job_failure"] == 1
    assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 0


def test_bad_input_does_not_block_following_valid_job(conn, config):
    queued(conn)
    original_digest = conn.execute("SELECT input_digest FROM jobs").fetchone()[0]
    conn.execute("UPDATE jobs SET input_digest='damaged'")
    conn.execute("""INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,attempt_no,
                 available_at,created_at,updated_at,context_json,lifecycle_round,priority)
                 SELECT 'job-2',case_id,job_type,state,?,attempt_no,available_at,
                 created_at,updated_at,context_json,lifecycle_round,priority+1 FROM jobs WHERE job_id='job-1'""",
                 (original_digest,))
    conn.execute("INSERT INTO broker_inputs SELECT 'job-2',payload_json,created_at FROM broker_inputs WHERE job_id='job-1'")
    conn.execute("UPDATE broker_inputs SET payload_json='{}' WHERE job_id='job-1'")
    result = claim_next(conn, active_config(config), worker_uid=UID, now=NOW)
    assert result["job_id"] == "job-2"
    assert conn.execute("SELECT state FROM jobs WHERE job_id='job-1'").fetchone()[0] == "waiting"
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
