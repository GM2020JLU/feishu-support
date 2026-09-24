from uuid import uuid4

import pytest
from test_broker_board import (
    board_request as board_request,  # noqa: PLC0414 - pytest fixture re-export
)
from test_broker_claim import NOW, UID

from k3_support.broker_board import read, submit
from k3_support.broker_protocol import encode_response


@pytest.fixture
def board_query(conn, board_request):
    cfg, request, reader = board_request
    submit(conn, cfg, request, peer_uid=UID, contract_reader=reader, now=NOW)
    params = {k: v for k, v in request["params"].items() if k not in {"action", "session_id"}}
    params.update(board_request_id=request["request_id"], offset=0)
    return cfg, {"version": 1, "request_id": str(uuid4()), "method": "board_read", "params": params}, reader


def test_board_read_pending_and_unicode_output_are_bounded_and_readonly(conn, board_query):
    cfg, request, reader = board_query
    before = list(conn.iterdump())
    pending = read(conn, cfg, request, peer_uid=UID, contract_reader=reader, now=NOW)
    assert pending["state"] == "queued" and pending["next_offset"] is None
    assert pending["stdout"] == "" and pending["exit_code"] is None
    assert list(conn.iterdump()) == before
    target = request["params"]["board_request_id"]
    stdout, stderr = "🙂" * 5000, "\x01" * 6000
    conn.execute("INSERT INTO broker_board_results VALUES(?,?,?,?,?)", (target, 0, stdout, stderr, "fixture"))
    conn.execute("UPDATE broker_board_actions SET state='succeeded'")
    # Expired/released device occupancy must not require renewed device access
    # merely to read already captured output under the still-live task grant.
    conn.execute("DELETE FROM locks WHERE lock_key='board1'")
    before = list(conn.iterdump())
    chunks = []
    while True:
        result = read(conn, cfg, request, peer_uid=UID, contract_reader=reader, now=NOW)
        assert not result["board_cleanup_verified"]
        assert len(encode_response(request_id=request["request_id"], result=result)) <= 262148
        chunks.append((result["stdout"], result["stderr"]))
        if result["next_offset"] is None:
            break
        request["params"]["offset"] = result["next_offset"]
    assert "".join(c[0] for c in chunks) == stdout and "".join(c[1] for c in chunks) == stderr
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("fault", ["target", "peer", "revoke", "offset", "boolean", "extra"])
def test_board_read_rejects_unbound_or_invalid_query(conn, board_query, fault):
    cfg, request, reader = board_query
    if fault == "target":
        request["params"]["board_request_id"] = str(uuid4())
    elif fault == "peer":
        conn.execute("UPDATE broker_board_actions SET peer_uid=?", (UID+1,))
    elif fault == "revoke":
        conn.execute("UPDATE broker_grants SET revoked_at='fixture'")
    elif fault == "offset":
        request["params"]["offset"] = 1
    elif fault == "boolean":
        request["params"]["offset"] = True
    else:
        request["params"]["session_id"] = "forged"
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        read(conn, cfg, request, peer_uid=UID, contract_reader=reader, now=NOW)
    assert list(conn.iterdump()) == before
