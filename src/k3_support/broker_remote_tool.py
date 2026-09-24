"""Worker-side remote tool: task-scoped IPC only, no SSH or database access."""

import argparse
import json
import os
import sys

from .broker_client import request_at
from .broker_protocol import decode_request
from .ids import canonical_json


def invoke(*, operation, request_id, command=None, mode=None, repo=None,
           remote_request_id=None, offset=0, environment=None, transport=request_at,
           session_id=None, action=None, board_request_id=None, after_id=""):
    env = os.environ if environment is None else environment
    raw = env.get("K3_SUPPORT_BROKER_TASK", "")
    if not raw or len(raw.encode()) > 8192:
        raise ValueError("worker task binding unavailable")
    # Validate the binding independently: reject extra authority fields from env.
    task = decode_request(canonical_json({"version": 1, "request_id": request_id,
                                         "method": "input", "params": json.loads(raw)}).encode())["params"]
    if operation == "submit":
        method, extra = "remote_submit", {"remote": {"mode": mode, "repo": repo, "command": command}}
    elif operation == "read":
        method, extra = "remote_read", {"remote_request_id": remote_request_id, "offset": offset}
    elif operation == "board_submit":
        method, extra = "board_submit", {"session_id": session_id, "action": action}
    elif operation == "board_read":
        method, extra = "board_read", {"board_request_id": board_request_id, "offset": offset}
    elif operation == "verification_list":
        method, extra = "verification_list", {"after_id": after_id}
    else:
        raise ValueError("unsupported remote operation")
    request = {"version": 1, "request_id": request_id, "method": method, "params": {**task, **extra}}
    decode_request(canonical_json(request).encode())
    response = transport(env["K3_SUPPORT_BROKER_SOCKET"], request,
                         control_uid=int(env["K3_SUPPORT_BROKER_CONTROL_UID"]), timeout=5)
    if response.get("ok") is not True:
        raise ValueError("broker rejected remote operation")
    return response["result"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    submit = commands.add_parser("submit", help="Read one shell command from stdin; never retry automatically")
    submit.add_argument("--request-id", required=True)
    submit.add_argument("--mode", choices=("inspect", "work"), required=True)
    submit.add_argument("--repo")
    read = commands.add_parser("read", help="Read an existing remote operation without resubmitting")
    read.add_argument("--request-id", required=True)
    read.add_argument("--remote-request-id", required=True)
    read.add_argument("--offset", type=int, default=0)
    verification = commands.add_parser("verification_list", help="Read prepared verification requests for this task; no execution")
    verification.add_argument("--request-id", required=True)
    verification.add_argument("--after-id", default="")
    args = vars(parser.parse_args(argv))
    try:
        if args["operation"] == "submit":
            raw = sys.stdin.buffer.read(32769)
            if len(raw) > 32768:
                raise ValueError("command too large")
            args["command"] = raw.decode("utf-8")
        print(json.dumps(invoke(**args), ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print("Remote request failed or delivery is unknown. Keep the original request ID; do not resubmit with a new ID.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
