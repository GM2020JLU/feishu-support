import json
from uuid import uuid4

import pytest
from test_broker_board import (
    board_request as board_request,  # noqa: PLC0414 - pytest fixture re-export
)
from test_broker_input import seeded
from test_broker_remote_runner import (
    remote as remote,  # noqa: PLC0414 - pytest fixture re-export
)

from k3_support.broker_remote_tool import invoke, main


def test_worker_board_bridge_rechecks_real_control_authorization(conn, board_request):
    from test_broker_claim import NOW, UID

    from k3_support.broker_board import read, submit
    cfg, request, reader = board_request
    task = {k: v for k, v in request["params"].items() if k not in ("action", "session_id")}
    env = {"K3_SUPPORT_BROKER_TASK": json.dumps(task), "K3_SUPPORT_BROKER_SOCKET": "/synthetic/broker.sock",
           "K3_SUPPORT_BROKER_CONTROL_UID": "4321"}
    def transport(path, rpc, **kwargs):
        assert path == env["K3_SUPPORT_BROKER_SOCKET"] and kwargs["control_uid"] == 4321
        handler = {"board_submit": submit, "board_read": read}[rpc["method"]]
        return {"ok": True, "result": handler(conn, cfg, rpc, peer_uid=UID, contract_reader=reader, now=NOW)}
    assert invoke(operation="board_submit", request_id=request["request_id"],
                  session_id=request["params"]["session_id"], action={"type": "reset"},
                  environment=env, transport=transport)["state"] == "queued"
    def fetch():
        return invoke(operation="board_read", request_id=str(uuid4()), board_request_id=request["request_id"],
                      environment=env, transport=transport)
    assert fetch()["state"] == "queued"
    conn.execute("UPDATE broker_grants SET revoked_at='revoked'")
    with pytest.raises(ValueError):
        fetch()
    assert conn.execute("SELECT count(*) FROM action_ledger").fetchone()[0] == 0


def test_worker_remote_tool_uses_only_bound_rpc(conn):
    task = seeded(conn)["params"]
    env = {"K3_SUPPORT_BROKER_TASK": json.dumps(task), "K3_SUPPORT_BROKER_SOCKET": "/synthetic/broker.sock",
           "K3_SUPPORT_BROKER_CONTROL_UID": "4321"}
    calls = []
    def transport(path, request, **kw):
        calls.append(request)
        assert path == env["K3_SUPPORT_BROKER_SOCKET"] and kw["control_uid"] == 4321
        return {"ok": True, "result": {"state": "queued"}}
    request_id = str(uuid4())
    assert invoke(operation="submit", request_id=request_id, mode="inspect", command="git status",
                  environment=env, transport=transport) == {"state": "queued"}
    assert calls[0]["params"]["lease_token"] == task["lease_token"]
    invoke(operation="read", request_id=str(uuid4()), remote_request_id=request_id,
           environment=env, transport=transport)
    assert [item["method"] for item in calls] == ["remote_submit", "remote_read"]
    def broken(*a, **kw):
        calls.append("failed")
        raise OSError("private transport diagnostic")
    with pytest.raises(OSError):
        invoke(operation="read", request_id=str(uuid4()), remote_request_id=request_id,
               environment=env, transport=broken)
    assert calls.count("failed") == 1
    for raw in ("", "{}", json.dumps({**task, "admin": True})):
        with pytest.raises(ValueError):
            invoke(operation="read", request_id=str(uuid4()), remote_request_id=request_id,
                   environment={**env, "K3_SUPPORT_BROKER_TASK": raw},
                   transport=lambda *a, **kw: pytest.fail("invalid task reached IPC"))


def test_remote_tool_errors_never_print_task_authority(monkeypatch, capsys):
    monkeypatch.setenv("K3_SUPPORT_BROKER_TASK", "private-secret-invalid-json")
    assert main(["read", "--request-id", str(uuid4()), "--remote-request-id", str(uuid4())]) == 1
    result = capsys.readouterr()
    assert result.out == "" and "private-secret" not in result.err


def test_codex_remote_instructions_do_not_embed_binding(tmp_path, monkeypatch):
    from k3_support.broker_codex import executor
    calls = []
    monkeypatch.setattr("k3_support.broker_codex.run_process", lambda **kw: calls.append(kw) or "report")
    callback = executor(executable="/synthetic/codex", workdir=tmp_path,
                        environment={"K3_SUPPORT_BROKER_TASK": "private-task-authority"}, remote_python="/opt/private/python")
    callback({"brief": "test", "model": "gpt-5.6-sol", "reasoning": "medium"}, lambda: None)
    assert b"k3_remote MCP remote_submit" in calls[0]["stdin"]
    assert any("broker_remote_mcp" in arg for arg in calls[0]["argv"])
    assert 'mcp_servers.k3_remote.required=true' in calls[0]["argv"]
    assert b"private-task-authority" not in calls[0]["stdin"]
    assert "private-task-authority" not in str(calls[0]["argv"])


def test_worker_tool_reads_control_consumer_output_without_database_access(conn, remote):
    from test_broker_claim import UID

    from k3_support.broker_claim_receipts import claim
    from k3_support.broker_remote import read
    from k3_support.broker_remote_runner import run_one
    cfg, reader = remote
    claim_id = conn.execute("SELECT request_id FROM broker_claim_receipts").fetchone()[0]
    task = claim(conn, cfg, {"version": 1, "request_id": claim_id, "method": "claim", "params": {"pool": "debug"}},
                 peer_uid=UID, control_key=b"t"*32)["task"]
    target = conn.execute("SELECT request_id FROM broker_remote_actions").fetchone()[0]
    env = {"K3_SUPPORT_BROKER_TASK": json.dumps(task), "K3_SUPPORT_BROKER_SOCKET": "/synthetic/broker.sock",
           "K3_SUPPORT_BROKER_CONTROL_UID": "4321"}
    def transport(path, request, **kw):
        return {"ok": True, "result": read(conn, cfg, request, peer_uid=UID, contract_reader=reader)}
    def fetch(offset=0):
        return invoke(operation="read", request_id=str(uuid4()), remote_request_id=target,
                      offset=offset, environment=env, transport=transport)
    assert fetch()["state"] == "queued"
    run_one(conn, cfg, contract_reader=reader,
            transport=lambda **kw: {"exit_code": 0, "stdout": "x"*5000, "stderr": "warning"})
    first = fetch()
    assert first["state"] == "succeeded" and first["stderr"] == "warning"
    assert first["stdout"] + fetch(first["next_offset"])["stdout"] == "x"*5000
