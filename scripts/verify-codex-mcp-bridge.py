"""Exercise actual Codex MCP discovery/call with no model turn or credentials."""

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from k3_support.broker_codex import executor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--authorized-fixture", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="codex-mcp-bridge-") as directory, ExitStack() as stack:
        root = Path(directory)
        authority, remote_target = {}, str(uuid4())
        if args.authorized_fixture:
            from codex_mcp_fixture import control_fixture

            authority, remote_target = stack.enter_context(control_fixture(root))
        seen = []
        with patch("k3_support.broker_codex.run_process", lambda **kw: seen.append(kw) or "unused"):
            executor(executable=args.codex, workdir=root, environment={}, remote_python=sys.executable)(
                {"model": "gpt-5.6-sol", "reasoning": "medium", "brief": "synthetic"}, lambda: None)
        original = seen[0]["argv"]
        settings = [part for i, arg in enumerate(original) if arg == "-c" for part in original[i:i+2]]
        settings += ["-c", 'model_provider="offline_fixture"', "-c", 'model_providers.offline_fixture.name="Offline fixture"',
                     "-c", 'model_providers.offline_fixture.base_url="http://127.0.0.1:9"',
                     "-c", 'model_providers.offline_fixture.wire_api="responses"',
                     "-c", 'analytics.enabled=false']
        messages = queue.Queue()
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([args.codex, "app-server", "--stdio", *settings], cwd=root,
                                       env={"HOME": directory, "CODEX_HOME": directory, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **authority},
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, start_new_session=True)
            def read():
                for raw in iter(process.stdout.readline, b""):
                    messages.put(json.loads(raw))
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            def send(value):
                process.stdin.write((json.dumps(value)+"\n").encode())
                process.stdin.flush()
            def call(ident, method, params):
                send({"id": ident, "method": method, "params": params})
                deadline = time.monotonic()+20
                while True:
                    value = messages.get(timeout=max(.01, deadline-time.monotonic()))
                    if value.get("id") == ident:
                        if "error" in value:
                            raise ValueError(value["error"])
                        return value["result"]
                    if time.monotonic() >= deadline:
                        raise TimeoutError(method)
            try:
                call(1, "initialize", {"clientInfo": {"name": "k3_offline_canary", "version": "0.1.0"},
                                       "capabilities": {"experimentalApi": True}})
                send({"method": "initialized"})
                thread = call(2, "thread/start", {"cwd": directory, "ephemeral": True, "model": "gpt-5.6-sol",
                                                  "modelProvider": "offline_fixture", "approvalPolicy": "never", "sandbox": "workspace-write"})
                tid = thread["thread"]["id"]
                listing = call(3, "mcpServerStatus/list", {"threadId": tid, "detail": "toolsAndAuthOnly"})
                target = next(item for item in listing["data"] if item["name"] == "k3_remote")
                assert {"remote_submit", "remote_read", "board_submit", "board_read"} <= set(target["tools"])
                result = call(4, "mcpServer/tool/call", {"threadId": tid, "server": "k3_remote", "tool": "remote_read",
                              "arguments": {"request_id": str(uuid4()), "remote_request_id": remote_target}})
                if args.authorized_fixture:
                    assert "synthetic-authorized-output" in json.dumps(result), result
                    board_result = call(7, "mcpServer/tool/call", {"threadId": tid, "server": "k3_remote", "tool": "board_read",
                                        "arguments": {"request_id": str(uuid4()), "board_request_id": remote_target}})
                    assert "synthetic-board-output" in json.dumps(board_result), board_result
                    refused = call(10, "mcpServer/tool/call", {"threadId": tid, "server": "k3_remote", "tool": "board_submit",
                                   "arguments": {"request_id": str(uuid4()), "session_id": "wrong-session", "action": {"type": "reset"}}})
                    assert "Broker request rejected or delivery unknown" in json.dumps(refused), refused
                    submitted = str(uuid4())
                    for ident in (8, 9):
                        queued = call(ident, "mcpServer/tool/call", {"threadId": tid, "server": "k3_remote", "tool": "board_submit",
                                      "arguments": {"request_id": submitted, "session_id": "synthetic-session", "action": {"type": "reset"}}})
                        assert "queued" in json.dumps(queued) and submitted in json.dumps(queued), queued
                    result = call(5, "mcpServer/tool/call", {"threadId": tid, "server": "k3_remote", "tool": "remote_read",
                                  "arguments": {"request_id": str(uuid4()), "remote_request_id": remote_target}})
                assert "Broker request rejected or delivery unknown" in json.dumps(result), result
                board_result = call(6, "mcpServer/tool/call", {"threadId": tid, "server": "k3_remote", "tool": "board_read",
                                    "arguments": {"request_id": str(uuid4()), "board_request_id": remote_target}})
                assert "Broker request rejected or delivery unknown" in json.dumps(board_result), board_result
                print(json.dumps({"codex_tool_discovery": True, "codex_tool_call": True,
                                  "board_tool_call_denied_without_live_authority": True,
                                  "authorized_read_then_revocation": args.authorized_fixture,
                                  "authorized_board_read_then_revocation": args.authorized_fixture,
                                  "authorized_board_submit_idempotent_no_device_io": args.authorized_fixture,
                                  "wrong_board_session_rejected": args.authorized_fixture,
                                  "missing_authority_rejected": not args.authorized_fixture,
                                  "control_db_read_denied": args.authorized_fixture,
                                  "revoked_authority_rejected": args.authorized_fixture, "model_turn_started": False,
                                  "production_state_accessed": False}))
            finally:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                reader.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
