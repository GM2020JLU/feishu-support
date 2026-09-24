import sqlite3
import json
from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID, queued
from test_review import active_config

from k3_support.broker_claim_receipts import claim
from k3_support.broker_execution_instances import register
from k3_support.broker_start import authorize


def bound(conn, config, *, board_session=None):
    queued(conn)
    if board_session is not None:
        from k3_support.ids import canonical_json, digest
        payload = json.loads(conn.execute("SELECT payload_json FROM broker_inputs WHERE job_id='job-1'").fetchone()[0])
        payload['context_extra']['board_session_id'] = board_session
        conn.execute("UPDATE broker_inputs SET payload_json=? WHERE job_id='job-1'", (canonical_json(payload),))
        conn.execute("UPDATE jobs SET input_digest=?,context_json=json_set(context_json,'$.board_session_id',?) WHERE job_id='job-1'", (digest(payload), board_session))
    cfg = active_config(config)
    request_id = str(uuid4())
    response = claim(conn, cfg, {"version": 1, "request_id": request_id,
                                "method": "claim", "params": {"pool": "debug"}},
                     peer_uid=UID, control_key=b"t" * 32, now=NOW)
    authorize(conn, cfg, {"version": 1, "request_id": str(uuid4()),
                         "method": "start", "params": response["task"]}, peer_uid=UID, now=NOW)
    grant_id = conn.execute("SELECT grant_id FROM broker_execution_starts").fetchone()[0]
    return {"grant_id": grant_id, "claim_request_id": request_id, "invocation_id": "a" * 32,
            "cgroup_path": f"/system.slice/k3-support-broker-worker@{request_id}.service"}


def test_instance_is_immutable_and_not_exit_evidence(conn, config):
    args = bound(conn, config)
    jobs = [tuple(row) for row in conn.execute("SELECT * FROM jobs")]
    assert register(conn, **args) == {"registered": True, "replayed": False}
    assert register(conn, **args) == {"registered": True, "replayed": True}
    with pytest.raises(ValueError, match="already bound"):
        register(conn, **{**args, "invocation_id": "b" * 32})
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE broker_execution_instances SET invocation_id=?", ("c" * 32,))
    assert [tuple(row) for row in conn.execute("SELECT * FROM jobs")] == jobs
    assert conn.execute("SELECT count(*) FROM execution_exit_receipts").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_retention_requires_bound_worker_exit_not_terminal_job_state(conn, config):
    from k3_support.case_content_inventory import preview
    args = bound(conn, config)
    cid = conn.execute('SELECT case_id FROM jobs LIMIT 1').fetchone()[0]
    conn.execute("UPDATE jobs SET state='failed',lease_owner=NULL,lease_expires_at=NULL")
    def held():
        return any(row['reason']=='worker_exit_unverified' for row in preview(conn,cid)['observed_holds'])
    assert held()
    register(conn, **args)
    assert held()
    conn.execute('INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)',
                 (args['grant_id'],args['invocation_id'],1234,1,0,'fixture'))
    assert not held()


@pytest.mark.parametrize("field,value", [
    ("grant_id", "missing"), ("claim_request_id", "invalid"),
    ("invocation_id", "0" * 32), ("invocation_id", "self-reported"),
    ("cgroup_path", "/"), ("cgroup_path", "/user.slice/desktop.scope"),
])
def test_invalid_instance_registration_has_no_writes(conn, config, field, value):
    args = bound(conn, config)
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        register(conn, **{**args, field: value})
    assert list(conn.iterdump()) == before


def test_another_claim_cannot_register_existing_attempt(conn, config):
    args = bound(conn, config)
    other = str(uuid4())
    with pytest.raises(ValueError, match="claim"):
        register(conn, **{**args, "claim_request_id": other,
                          "cgroup_path": f"/system.slice/k3-support-broker-worker@{other}.service"})
    assert conn.execute("SELECT count(*) FROM broker_execution_instances").fetchone()[0] == 0
