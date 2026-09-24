from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_broker_claim import UID, queued
from test_broker_receipts import setup
from test_review import active_config
from test_semantic_budget import policy

from k3_support.broker_claim import claim_next
from k3_support.broker_claim_receipts import claim
from k3_support.broker_execution_contract import ExecutionContract
from k3_support.broker_renew import renew
from k3_support.broker_start import authorize

NOW = datetime(2026, 9, 8, 0, 30, tzinfo=UTC)


def test_budgeted_instance_does_not_claim_without_budget_handoff(conn, config):
    queued(conn)
    policy(conn)
    before = list(conn.iterdump())
    assert claim_next(conn, active_config(config), worker_uid=UID, now=NOW) is None
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("operation", ["start", "renew", "replay_renew"])
def test_policy_enabled_after_claim_blocks_start_and_renew(conn, config, operation):
    request = setup(conn)
    cfg = active_config(config)
    if operation == "replay_renew":
        renew(conn, request, peer_uid=1234, now=NOW, config=cfg)
    policy(conn)
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        if operation == "start":
            authorize(conn, cfg, {**request, "method": "start"}, peer_uid=1234, now=NOW)
        else:
            renew(conn, request, peer_uid=1234, now=NOW, config=cfg)
    assert list(conn.iterdump()) == before
    assert conn.execute("SELECT count(*) FROM broker_execution_starts").fetchone()[0] == 0


def test_claim_replay_cannot_bypass_new_budget_policy(conn, config):
    queued(conn)
    cfg = active_config(config)
    request = {"version": 1, "request_id": str(uuid4()), "method": "claim", "params": {"pool": "debug"}}
    claim(conn, cfg, request, peer_uid=UID, control_key=b"t"*32, now=NOW)
    policy(conn)
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        claim(conn, cfg, request, peer_uid=UID, control_key=b"t"*32, now=NOW)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("failure", [None, "exhausted", "write_failure"])
def test_budget_and_start_commit_together(conn, config, failure):
    import sqlite3

    from test_broker_start_observation import synthetic_observer

    from k3_support import model_budget
    from k3_support.ids import digest

    queued(conn)
    cfg = active_config(config)
    descriptor = ExecutionContract("fixture", "https://example.com/v1", "gpt-5.6-sol", "medium", "responses", "a" * 64)
    policy(conn)
    request = {"version": 1, "request_id": str(uuid4()), "method": "claim", "params": {"pool": "debug"}}
    task = claim(conn, cfg, request, peer_uid=UID, control_key=b"t"*32, now=NOW,
                 contract_reader=lambda: descriptor)["task"]
    if failure == "exhausted":
        limits = conn.execute("SELECT * FROM model_budget_policy").fetchone()
        model_budget.reserve(conn, request_id="prior", case_id=task["case_id"], provider="fixture", model="fixture",
                             amount=limits["attempt_limit"], input_digest=digest("prior"), at=NOW)
        conn.execute("UPDATE model_budget_policy SET daily_limit=attempt_limit,case_limit=attempt_limit")
    if failure == "write_failure":
        conn.execute("CREATE TRIGGER reject_budget_link BEFORE INSERT ON broker_budget_attempts "
                     "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    before = list(conn.iterdump())
    start = {**request, "request_id": str(uuid4()), "method": "start",
             "params": {**task, "contract_fingerprint": descriptor.fingerprint}}
    def launch():
        return authorize(conn, cfg, start, peer_uid=UID, now=NOW, observe_instance=synthetic_observer,
                         contract_reader=lambda: descriptor)
    if failure:
        with pytest.raises((ValueError, sqlite3.Error)):
            launch()
        assert list(conn.iterdump()) == before
    else:
        assert launch()["accepted"]
        row = conn.execute("SELECT a.* FROM broker_budget_attempts b JOIN model_budget_attempts a USING(attempt_id)").fetchone()
        assert row["state"] == "dispatched" and row["charged"] == row["reserved"] > 0
        assert row["provider"] == "fixture" and row["model"] == "gpt-5.6-sol"
        with pytest.raises(ValueError):
            launch()
        assert conn.execute("SELECT count(*) FROM model_budget_attempts").fetchone()[0] == 1
        heartbeat = {**start, "request_id": str(uuid4()), "method": "renew", "params": task}
        assert renew(conn, heartbeat, peer_uid=UID, now=NOW, config=cfg,
                     contract_reader=lambda: descriptor)["accepted"]
        conn.execute("UPDATE model_budget_policy SET revision=revision+1")
        before_replay = list(conn.iterdump())
        with pytest.raises(ValueError):
            renew(conn, heartbeat, peer_uid=UID, now=NOW, config=cfg,
                  contract_reader=lambda: descriptor)
        assert list(conn.iterdump()) == before_replay
        # Restore the synthetic policy to isolate manager-exit fencing.
        conn.execute("UPDATE model_budget_policy SET revision=revision-1")
        instance = conn.execute("SELECT grant_id,invocation_id FROM broker_execution_instances").fetchone()
        conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                     (instance["grant_id"], instance["invocation_id"], 1234, 1, 0, NOW.isoformat()))
        before_exit_replay = list(conn.iterdump())
        with pytest.raises(ValueError, match="exited"):
            renew(conn, heartbeat, peer_uid=UID, now=NOW, config=cfg,
                  contract_reader=lambda: descriptor)
        assert list(conn.iterdump()) == before_exit_replay
