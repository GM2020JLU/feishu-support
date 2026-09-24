from datetime import UTC, datetime

import pytest
from test_broker_connection import exchange
from test_broker_receipts import setup
from test_review import active_config, make_job

from k3_support.broker_input import read
from k3_support.ids import canonical_json, digest


def seeded(conn, *, board_session_id=None):
    request = setup(conn)
    request["method"] = "input"
    payload = {"case_id": request["params"]["case_id"], "lifecycle_round": 1,
               "brief": "untrusted test input", "repos": ["u-boot"],
               "model": "gpt-5.6-sol", "reasoning": "medium",
               "context_extra": {"private": "do-not-project"}}
    if board_session_id is not None:
        payload["context_extra"]["board_session_id"] = board_session_id
    bound = digest(payload)
    conn.execute("UPDATE jobs SET input_digest=?", (bound,))
    conn.execute("UPDATE broker_grants SET input_digest=?", (bound,))
    conn.execute("INSERT INTO broker_inputs VALUES(?,?,?)",
                 ("job-1", canonical_json(payload), "now"))
    request["params"]["input_digest"] = bound
    return request


def test_input_socket_projects_only_worker_fields(conn, monkeypatch):
    request = seeded(conn)
    before = list(conn.iterdump())
    result = exchange(conn, monkeypatch, request)["result"]
    assert set(result) == {"job_id", "input_digest", "brief", "repos", "model", "reasoning"}
    assert "do-not-project" not in str(result)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("change", [
    "DELETE FROM broker_inputs",
    "UPDATE broker_inputs SET payload_json='{}'",
    "UPDATE cases SET lifecycle_round=2",
])
def test_missing_or_modified_input_is_rejected(conn, change):
    request = seeded(conn)
    conn.execute(change)
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        read(conn, request, peer_uid=1234, now=datetime(2026, 9, 8, 0, 30, tzinfo=UTC))
    assert list(conn.iterdump()) == before


def test_real_job_creation_persists_digest_bound_snapshot(conn, config):
    make_job(conn, active_config(config))
    row = conn.execute("SELECT j.input_digest,b.payload_json FROM jobs j JOIN broker_inputs b USING(job_id)").fetchone()
    import json
    assert row is not None
    assert digest(json.loads(row["payload_json"])) == row["input_digest"]
