from uuid import uuid4

import pytest
from test_attention_races import race
from test_broker_claim import NOW, UID, queued
from test_broker_connection import exchange
from test_review import active_config

from k3_support.broker_claim_receipts import claim

KEY = b"test" * 8


def request():
    return {"version": 1, "request_id": str(uuid4()), "method": "claim", "params": {"pool": "debug"}}


def test_claim_replay_reconstructs_same_secret_without_storing_it(conn, config):
    queued(conn)
    value = request()
    cfg = active_config(config)
    first = claim(conn, cfg, value, peer_uid=UID, control_key=KEY, now=NOW)
    before = list(conn.iterdump())
    assert first["task"]["lease_token"] not in "\n".join(before)
    assert KEY.decode() not in "\n".join(before)
    assert claim(conn, cfg, value, peer_uid=UID, control_key=KEY, now=NOW) == first
    assert list(conn.iterdump()) == before
    with pytest.raises(ValueError):
        claim(conn, cfg, value, peer_uid=UID, control_key=b"x" * 32, now=NOW)
    assert list(conn.iterdump()) == before


def test_concurrent_replay_returns_same_assignment(conn, config):
    queued(conn)
    value = request()
    cfg = active_config(config)
    def run(db):
        return claim(db, cfg, value, peer_uid=UID, control_key=KEY, now=NOW)
    left, right = race(config, run, run)
    assert left == right and left["task"] is not None
    assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM broker_claim_receipts").fetchone()[0] == 1


def test_empty_claim_replay_does_not_claim_later_job(conn, config):
    cfg = active_config(config)
    value = request()
    assert claim(conn, cfg, value, peer_uid=UID, control_key=KEY, now=NOW) == {"task": None}
    queued(conn)
    assert claim(conn, cfg, value, peer_uid=UID, control_key=KEY, now=NOW) == {"task": None}
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "queued"


def test_cancelled_claim_cannot_be_recovered(conn, config):
    queued(conn)
    cfg = active_config(config)
    value = request()
    claim(conn, cfg, value, peer_uid=UID, control_key=KEY, now=NOW)
    conn.execute("UPDATE jobs SET state='cancelled'")
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        claim(conn, cfg, value, peer_uid=UID, control_key=KEY, now=NOW)
    assert list(conn.iterdump()) == before


def test_claim_socket_returns_replayable_task(conn, config, monkeypatch):
    queued(conn)
    value = request()
    options = {"config": active_config(config), "control_key": KEY}
    first = exchange(conn, monkeypatch, value, **options)
    assert first["result"]["task"]["job_id"] == "job-1"
    before = list(conn.iterdump())
    assert exchange(conn, monkeypatch, value, **options) == first
    assert list(conn.iterdump()) == before
