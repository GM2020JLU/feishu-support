from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID
from test_broker_execution_instances import bound
from test_review import active_config

from k3_support.broker_execution_contract import ExecutionContract
from k3_support.broker_remote import submit


@pytest.mark.parametrize("mutation", [None, "other_repo", "cancelled"])
def test_remote_queue_is_scoped_idempotent_and_does_not_execute(conn, config, mutation, monkeypatch):
    import json
    args = bound(conn, config)
    descriptor = ExecutionContract("fixture", "https://example.com", "gpt-5.6-sol", "medium", "responses", "a"*64)
    conn.execute("INSERT INTO broker_execution_contracts VALUES(?,?,?,?,?)",
                 (args["grant_id"], descriptor.fingerprint, descriptor.provider, descriptor.model, "synthetic"))
    from k3_support.broker_claim_receipts import claim
    response = claim(conn, active_config(config), {"version": 1,"request_id": args["claim_request_id"],
                     "method":"claim","params":{"pool":"debug"}}, peer_uid=UID, control_key=b"t"*32, now=NOW)
    request = {"version":1,"request_id":str(uuid4()),"method":"remote_submit",
               "params":{**response["task"],"remote":{"mode":"work","repo":"u-boot","command":"git status --short"}}}
    if mutation == "other_repo":
        request["params"]["remote"]["repo"] = "outside"
    if mutation == "cancelled":
        conn.execute("UPDATE jobs SET state='cancelled'")
    monkeypatch.setattr("subprocess.Popen", lambda *a,**kw: pytest.fail("submission must not launch processes"))
    before = list(conn.iterdump())
    if mutation:
        with pytest.raises(ValueError):
            submit(conn, active_config(config), request, peer_uid=UID, contract_reader=lambda:descriptor, now=NOW)
        assert list(conn.iterdump()) == before
    else:
        result = submit(conn, active_config(config), request, peer_uid=UID, contract_reader=lambda:descriptor, now=NOW)
        assert result["state"] == "queued"
        assert submit(conn, active_config(config), request, peer_uid=UID, contract_reader=lambda:descriptor, now=NOW) == result
        row = conn.execute("SELECT plan_json FROM broker_remote_actions").fetchone()
        assert response["task"]["lease_token"] not in row[0]
        command = json.loads(row[0])["command"]
        assert "--unshare-net" in command
        assert "/investigation-job-1" in command


def test_remote_read_is_bound_paginated_and_read_only(conn, config):
    from k3_support.broker_claim_receipts import claim
    from k3_support.broker_protocol import encode_response
    from k3_support.broker_remote import read
    args = bound(conn, config)
    descriptor = ExecutionContract("fixture", "https://example.com", "gpt-5.6-sol", "medium", "responses", "a"*64)
    conn.execute("INSERT INTO broker_execution_contracts VALUES(?,?,?,?,?)",
                 (args["grant_id"], descriptor.fingerprint, descriptor.provider, descriptor.model, "synthetic"))
    cfg = active_config(config)
    task = claim(conn, cfg, {"version": 1, "request_id": args["claim_request_id"], "method": "claim",
                            "params": {"pool": "debug"}}, peer_uid=UID, control_key=b"t"*32, now=NOW)["task"]
    target = str(uuid4())
    submit(conn, cfg, {"version": 1, "request_id": target, "method": "remote_submit",
                      "params": {**task, "remote": {"mode": "inspect", "repo": None, "command": "true"}}},
           peer_uid=UID, contract_reader=lambda: descriptor, now=NOW)
    query = {"version": 1, "request_id": str(uuid4()), "method": "remote_read",
             "params": {**task, "remote_request_id": target, "offset": 0}}
    def fetch(value=query, uid=UID):
        return read(conn, cfg, value, peer_uid=uid, contract_reader=lambda: descriptor, now=NOW)
    assert fetch()["state"] == "queued"
    assert fetch()["exit_code"] is None
    output = "\x00" * 9000
    conn.execute("INSERT INTO broker_remote_results VALUES(?,?,?,?,?)", (target, 0, output, "err", "synthetic"))
    conn.execute("UPDATE broker_remote_actions SET state='succeeded'")
    before = list(conn.iterdump())
    joined = ""
    while True:
        page = fetch()
        assert len(encode_response(request_id=query["request_id"], result=page)) < 65536
        joined += page["stdout"]
        if page["next_offset"] is None:
            break
        query["params"]["offset"] = page["next_offset"]
    assert joined == output
    for field, value in (("remote_request_id", str(uuid4())), ("execution_round", 999), ("offset", -1), ("offset", True)):
        with pytest.raises(ValueError):
            fetch({**query, "params": {**query["params"], field: value}})
    with pytest.raises(ValueError):
        fetch(uid=UID+1)
    assert list(conn.iterdump()) == before
    conn.execute("UPDATE jobs SET state='cancelled'")
    with pytest.raises(ValueError):
        fetch()
