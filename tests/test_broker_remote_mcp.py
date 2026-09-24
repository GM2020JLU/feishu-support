import io
import json
import subprocess
import sys
from uuid import uuid4

import pytest

from k3_support.broker_remote_mcp import Bridge, serve


def init(bridge):
    response = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_bridge_only_exposes_scoped_tools_and_never_retries():
    calls = []
    def invoke(**kw):
        calls.append(kw)
        raise OSError("private-secret")
    bridge = Bridge(invoke)
    request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    assert "error" in bridge.handle(request)
    init(bridge)
    assert [tool["name"] for tool in bridge.handle(request)["result"]["tools"]] == ["verification_list", "remote_submit", "remote_read", "board_submit", "board_read"]
    request.update(method="tools/call", params={"name": "remote_submit", "arguments": {"request_id": str(uuid4()), "mode": "inspect", "command": "true"}})
    response = bridge.handle(request)
    assert response["result"]["isError"] and len(calls) == 1
    assert "private-secret" not in json.dumps(response)
    request["params"]["arguments"]["environment"] = {"admin": "true"}
    assert bridge.handle(request)["error"]["code"] == -32602
    assert len(calls) == 1


def test_frames_reject_duplicates_oversize_and_never_echo_payload():
    output = io.BytesIO()
    assert serve(io.BytesIO(b'{"id":1,"id":2,"secret":"private"}\n'), output) == 0
    assert "private" not in output.getvalue().decode()
    assert json.loads(output.getvalue())["error"]["code"] == -32700
    assert serve(io.BytesIO(b"x"*262145+b"\n"), io.BytesIO()) == 1


@pytest.mark.parametrize('arguments,diagnostic', [
    ({'request_id': 'PRIVATE', 'mode': 'inspect', 'command': 'true'}, 'request_id violates minLength'),
    ({'request_id': '01234567-89ab-cdef-0123-456789abcdef', 'mode': 'PRIVATE', 'command': 'true'}, 'mode violates enum'),
    ({'request_id': '01234567-89ab-cdef-0123-456789abcdef', 'mode': 'inspect', 'command': ['PRIVATE']}, 'command violates type'),
    ({'request_id': '01234567-89ab-cdef-0123-456789abcdef', 'mode': 'inspect', 'command': 'true', 'PRIVATE': 'secret'}, 'arguments violates additionalProperties'),
    ({'mode': 'inspect', 'command': 'PRIVATE'}, 'arguments violates required'),
    (['PRIVATE'], 'arguments violates type'),
])
def test_argument_diagnostics_never_echo_values_or_unknown_names(arguments, diagnostic):
    bridge = Bridge(lambda **_: pytest.fail('invalid arguments reached broker'))
    init(bridge)
    response = bridge.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                              'params': {'name': 'remote_submit', 'arguments': arguments}})
    assert response['error']['code'] == -32602
    assert diagnostic in response['error']['message']
    assert 'PRIVATE' not in json.dumps(response) and 'secret' not in json.dumps(response)


def test_actual_stdio_process_handshake_without_credentials():
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    result = subprocess.run([sys.executable, "-m", "k3_support.broker_remote_mcp"],
                            input="".join(json.dumps(f)+"\n" for f in frames), capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0 and result.stderr == ""
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(responses) == 2
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == {
        "verification_list", "remote_submit", "remote_read", "board_submit", "board_read",
    }


@pytest.mark.parametrize("action", [
    {"type": "list"}, {"type": "reset"}, {"type": "enter_brom"},
    {"type": "ram_boot", "uboot_only": True, "timeout": 300},
    {"type": "serial_wait", "regex": "U-Boot", "timeout": 30},
    {"type": "serial_exec", "command": ["version"], "expect": "=>", "timeout": 30},
])
def test_board_tools_dispatch_without_accepting_caller_authority(action):
    calls = []
    bridge = Bridge(lambda **kw: calls.append(kw) or {"state": "queued"})
    init(bridge)
    args = {"request_id": str(uuid4()), "session_id": "case-board-1", "action": action}
    def call(arguments):
        return bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "board_submit", "arguments": arguments}})
    assert call(args)["result"]["isError"] is False
    assert calls == [{"operation": "board_submit", **args}]
    for forged in ({**args, "environment": {}}, {**args, "lease_token": "forged"},
                   {**args, "action": {**action, "device": "/dev/ttyUSB0"}},
                   {**args, "action": {**action, "timeout": True}}):
        assert call(forged)["error"]["code"] == -32602
    assert len(calls) == 1


@pytest.mark.parametrize("mode,repo,valid", [
    ("inspect", None, True), ("inspect", "calculator", False),
    ("inspect", "<omitted>", True), ("work", "<omitted>", False),
    ("work", "calculator", True), ("work", None, False),
    ("work", "", False), ("work", "../private", False),
])
def test_remote_modes_reject_invalid_repo_before_delivery(mode, repo, valid):
    calls = []
    bridge = Bridge(lambda **kw: calls.append(kw) or {"state": "queued"})
    init(bridge)
    args = {"request_id": str(uuid4()), "mode": mode, "repo": repo, "command": "true"}
    if repo == "<omitted>":
        del args["repo"]
    result = bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "remote_submit", "arguments": args}})
    assert bool(calls) == valid
    if valid:
        assert result["result"]["isError"] is False
    else:
        assert result["error"]["code"] == -32602
        assert "private" not in json.dumps(result)


@pytest.mark.parametrize("request_id", ["a" * 36, "01234567-89AB-cdef-0123-456789abcdef"])
def test_noncanonical_uuid_is_not_an_unknown_delivery(request_id):
    bridge = Bridge(lambda **_: pytest.fail("invalid UUID reached broker"))
    init(bridge)
    result = bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "remote_submit", "arguments": {
            "request_id": request_id, "mode": "inspect", "command": "true"}}})
    assert result["error"]["code"] == -32602
    assert "request_id violates pattern" in result["error"]["message"]


def test_remote_submit_catalog_uses_client_compatible_root_schema():
    bridge = Bridge()
    init(bridge)
    tools = bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    submit = next(tool for tool in tools if tool["name"] == "remote_submit")
    assert set(submit["inputSchema"]) == {"type", "properties", "required", "additionalProperties"}
    assert "For inspect, omit repo or use null" in submit["description"]
