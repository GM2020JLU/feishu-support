from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_attention_races import race
from test_broker_claim import queued
from k3_support.broker_claim_receipts import claim
from test_review import active_config

from k3_support.broker_start import authorize

NOW = datetime(2026, 9, 8, 0, 30, tzinfo=UTC)


def setup(conn, config, *, board_session_id=None):
    queued(conn)
    if board_session_id is not None:
        import json
        from k3_support.ids import canonical_json, digest
        payload = json.loads(conn.execute("SELECT payload_json FROM broker_inputs").fetchone()[0])
        payload['context_extra']['board_session_id'] = board_session_id
        conn.execute("UPDATE broker_inputs SET payload_json=?", (canonical_json(payload),))
        conn.execute("UPDATE jobs SET input_digest=?,context_json=json_set(context_json,'$.board_session_id',?)", (digest(payload), board_session_id))
    response = claim(conn, active_config(config),
                     {"version": 1, "request_id": str(uuid4()), "method": "claim", "params": {"pool": "debug"}},
                     peer_uid=1234, control_key=b"t" * 32, now=NOW)
    return {"version": 1, "request_id": str(uuid4()), "method": "start", "params": response["task"]}


def test_duplicate_start_never_grants_second_launch(conn, config):
    request = setup(conn, config)
    request["method"] = "start"
    cfg = active_config(config)
    assert authorize(conn, cfg, request, peer_uid=1234, now=NOW)["accepted"] is True
    before = list(conn.iterdump())
    for request_id in (request["request_id"], str(uuid4())):
        with pytest.raises(ValueError, match="already authorized"):
            authorize(conn, cfg, {**request, "request_id": request_id}, peer_uid=1234, now=NOW)
        assert list(conn.iterdump()) == before


def test_two_connections_only_one_start_authorization(conn, config):
    request = setup(conn, config)
    request["method"] = "start"
    cfg = active_config(config)
    def start(db):
        try:
            authorize(db, cfg, request, peer_uid=1234, now=NOW)
            return True
        except ValueError:
            return False
    assert sorted(race(config, start, start)) == [False, True]
    assert conn.execute("SELECT count(*) FROM broker_execution_starts").fetchone()[0] == 1


def test_disabled_mode_cannot_authorize_start(conn, config):
    request = setup(conn, config)
    request["method"] = "start"
    with pytest.raises(ValueError, match="disabled"):
        authorize(conn, config, request, peer_uid=1234, now=NOW)
    assert conn.execute("SELECT count(*) FROM broker_execution_starts").fetchone()[0] == 0
