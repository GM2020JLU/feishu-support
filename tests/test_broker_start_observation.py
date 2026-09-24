from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID, queued
from test_review import active_config

from k3_support.broker_claim_receipts import claim
from k3_support.broker_execution_contract import ExecutionContract
from k3_support.broker_execution_instances import register
from k3_support.broker_start import authorize


def synthetic_observer(conn, *, grant_id, claim_request_id):
    # Synthetic manager evidence only, not a production systemd canary.
    return register(conn, grant_id=grant_id, claim_request_id=claim_request_id,
                    invocation_id=uuid4().hex,
                    cgroup_path=f"/system.slice/k3-support-broker-worker@{claim_request_id}.service")


@pytest.mark.parametrize("failure", [None, "unavailable", "cancelled", "no_registration"])
def test_start_waits_for_observation_and_rechecks_authority(conn, config, failure):
    queued(conn)
    cfg = active_config(config)
    task = claim(conn, cfg, {"version": 1, "request_id": str(uuid4()), "method": "claim",
                            "params": {"pool": "debug"}}, peer_uid=UID, control_key=b"t" * 32, now=NOW)["task"]
    request = {"version": 1, "request_id": str(uuid4()), "method": "start", "params": task}

    def observe(db, **kwargs):
        assert not db.in_transaction
        if failure == "unavailable":
            raise ValueError("manager unavailable")
        if failure == "no_registration":
            return
        synthetic_observer(db, **kwargs)
        if failure == "cancelled":
            db.execute("UPDATE jobs SET state='cancelled'")

    if failure:
        with pytest.raises(ValueError):
            authorize(conn, cfg, request, peer_uid=UID, now=NOW, observe_instance=observe)
    else:
        assert authorize(conn, cfg, request, peer_uid=UID, now=NOW, observe_instance=observe)["accepted"]
    assert conn.execute("SELECT count(*) FROM broker_execution_starts").fetchone()[0] == 1
    with pytest.raises(ValueError):
        authorize(conn, cfg, request, peer_uid=UID, now=NOW, observe_instance=observe)


@pytest.mark.parametrize("mode", ["match", "missing", "mismatch", "changed", "invalid_reader"])
def test_control_contract_required_and_rechecked(conn, config, mode):
    queued(conn)
    cfg = active_config(config)
    task = claim(conn, cfg, {"version": 1, "request_id": str(uuid4()), "method": "claim",
                            "params": {"pool": "debug"}}, peer_uid=UID, control_key=b"t" * 32, now=NOW)["task"]
    if mode != "missing":
        task = {**task, "contract_fingerprint": ("b" if mode == "mismatch" else "a") * 64}
    request = {"version": 1, "request_id": str(uuid4()), "method": "start", "params": task}
    fingerprint = "a" * 64

    def read_contract():
        if mode == "invalid_reader":
            return None
        return ExecutionContract("test", "https://example.com/v1", "gpt-5.6-sol", "medium", "responses", fingerprint)

    def observe(db, **kwargs):
        nonlocal fingerprint
        synthetic_observer(db, **kwargs)
        if mode == "changed":
            fingerprint = "b" * 64

    if mode == "match":
        assert authorize(conn, cfg, request, peer_uid=UID, now=NOW,
                         observe_instance=observe, contract_reader=read_contract)["accepted"]
    else:
        with pytest.raises(ValueError):
            authorize(conn, cfg, request, peer_uid=UID, now=NOW,
                      observe_instance=observe, contract_reader=read_contract)
    expected = int(mode in {"match", "changed"})
    assert conn.execute("SELECT count(*) FROM broker_execution_starts").fetchone()[0] == expected
    assert conn.execute("SELECT count(*) FROM broker_execution_contracts").fetchone()[0] == expected
