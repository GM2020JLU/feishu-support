# ruff: noqa: F811 -- pytest injects the explicitly imported fixture
"""Use the real worker bridge, scoped broker read and submission/consumer path."""

import json
from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID
from test_project_verification_runs import context  # noqa: F401 -- shared fixture

from k3_support import project_verification_runs as runs
from k3_support.broker_protocol import decode_request
from k3_support.broker_remote import submit
from k3_support.broker_remote_mcp import Bridge
from k3_support.broker_remote_runner import run_one
from k3_support.broker_remote_tool import invoke


def worker(conn, ctx):
    cfg, descriptor, *_, request = ctx
    task = {key: value for key, value in request["params"].items() if key != "remote"}
    env = {
        "K3_SUPPORT_BROKER_TASK": json.dumps(task),
        "K3_SUPPORT_BROKER_SOCKET": "/synthetic.sock",
        "K3_SUPPORT_BROKER_CONTROL_UID": "1000",
    }
    calls = []

    def transport(path, value, **kwargs):
        calls.append(value)
        handler = (
            runs.read_for_worker if value["method"] == "verification_list" else submit
        )
        result = handler(
            conn, cfg, value, peer_uid=UID, contract_reader=lambda: descriptor, now=NOW
        )
        return {"ok": True, "result": result}

    return lambda **kw: invoke(**kw, environment=env, transport=transport), calls, task


def test_worker_discovers_late_prepared_request_and_submits_exact_identity(
    conn, context
):
    call, calls, _ = worker(conn, context)
    # Input was already delivered when the worker was claimed; this is a live inbox.
    assert call(operation="verification_list", request_id=str(uuid4()))["items"] == []
    before_input = conn.execute("SELECT payload_json FROM broker_inputs").fetchone()[0]
    run = runs.prepare(conn, **context[-2])
    page = call(operation="verification_list", request_id=str(uuid4()))
    item = page["items"][0]
    assert item["run_id"] == run["run_id"] and item["dispatchable"] is True
    assert "grant_id" not in item and "lease_token" not in json.dumps(page)
    assert (
        conn.execute("SELECT payload_json FROM broker_inputs").fetchone()[0]
        == before_input
    )
    assert conn.execute("SELECT count(*) FROM broker_remote_actions").fetchone()[0] == 0
    call(operation="submit", request_id=item["remote_request_id"], **item["remote"])
    listed = call(operation="verification_list", request_id=str(uuid4()))["items"][0]
    assert listed["execution_state"] == "queued" and listed["dispatchable"] is False
    cfg, descriptor = context[:2]
    run_one(
        conn,
        cfg,
        contract_reader=lambda: descriptor,
        transport=lambda **kw: {"exit_code": 0, "stdout": "fixture", "stderr": ""},
    )
    listed = call(operation="verification_list", request_id=str(uuid4()))["items"][0]
    assert (
        listed["execution_state"] == "succeeded"
        and listed["verification_state"] == "unknown"
    )
    assert listed["dispatchable"] is False
    assert [c["method"] for c in calls].count("remote_submit") == 1


def test_worker_pagination_and_hold_are_readonly(conn, context):
    intent = context[-2]
    for i in range(3):
        runs.prepare(
            conn,
            **(intent | {"request_id": f"run-{i}", "remote_request_id": str(uuid4())}),
        )
    call, _, _ = worker(conn, context)
    before = list(conn.iterdump())
    ids = []
    cursor = ""
    while True:
        page = call(
            operation="verification_list", request_id=str(uuid4()), after_id=cursor
        )
        ids.extend(item["run_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(set(ids)) == 3 and list(conn.iterdump()) == before
    conn.execute("UPDATE project_bug_rounds SET execution_state='paused'")
    assert (
        call(operation="verification_list", request_id=str(uuid4()))["items"][0][
            "dispatchable"
        ]
        is False
    )


def test_worker_cannot_query_another_task_or_actor(conn, context):
    _, _, task = worker(conn, context)
    request = {
        "version": 1,
        "request_id": str(uuid4()),
        "method": "verification_list",
        "params": task | {"after_id": ""},
    }
    cfg, descriptor = context[:2]
    with pytest.raises(ValueError):
        runs.read_for_worker(
            conn,
            cfg,
            request,
            peer_uid=UID + 1,
            contract_reader=lambda: descriptor,
            now=NOW,
        )
    with pytest.raises(ValueError):
        decode_request(
            json.dumps(
                request
                | {"params": request["params"] | {"grant_id": context[-2]["grant_id"]}}
            ).encode()
        )
    with pytest.raises(ValueError):
        decode_request(
            json.dumps(
                request | {"params": request["params"] | {"after_id": True}}
            ).encode()
        )


def test_mcp_discovery_reads_without_submit(conn, context):
    call, calls, _ = worker(conn, context)
    runs.prepare(conn, **context[-2])
    bridge = Bridge(call)
    bridge.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    response = bridge.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "verification_list",
                "arguments": {"request_id": str(uuid4())},
            },
        }
    )
    assert response["result"]["isError"] is False
    content = json.loads(response["result"]["content"][0]["text"])
    assert content["items"][0]["dispatchable"] is True
    assert [c["method"] for c in calls] == ["verification_list"]


def test_verification_list_reaches_authenticated_socket_handler(
    conn, context, monkeypatch
):
    import socket
    import struct

    from k3_support.broker_connection import serve_connection
    from k3_support.broker_identity import Peer

    runs.prepare(conn, **context[-2])
    _, _, task = worker(conn, context)
    value = {
        "version": 1,
        "request_id": str(uuid4()),
        "method": "verification_list",
        "params": task | {"after_id": ""},
    }
    monkeypatch.setattr(
        "k3_support.broker_identity.authenticate_worker",
        lambda *a, **kw: Peer(pid=42, uid=UID, gid=UID),
    )
    before = list(conn.iterdump())
    left, right = socket.socketpair()
    with right:
        encoded = json.dumps(value).encode()
        right.sendall(struct.pack("!I", len(encoded)) + encoded)
        serve_connection(
            conn,
            left,
            worker_uid=UID,
            now=NOW,
            config=context[0],
            contract_reader=lambda: context[1],
        )
        right.settimeout(1)
        raw = b""
        while chunk := right.recv(4096):
            raw += chunk
    result = json.loads(raw[4:])
    assert result["ok"] is True and result["result"]["items"][0]["dispatchable"] is True
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("command", ["\x00", "\ud800", "验" * 16000])
def test_unsubmittable_command_cannot_be_prepared(conn, context, command):
    intent = context[-2]
    with pytest.raises(ValueError, match="broker protocol"):
        runs.prepare(
            conn, **(intent | {"remote": intent["remote"] | {"command": command}})
        )
    assert (
        conn.execute("SELECT count(*) FROM project_verification_runs").fetchone()[0]
        == 0
    )
