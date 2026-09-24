"""Control-only receipt observation. Never authorizes recovery or retries work."""

import json
import shlex
from importlib.resources import files
from pathlib import Path
from uuid import UUID

from .broker_process import run_process
from .execution_transport import matches, plan_argv
from .remote_guard import receipt_directory


def command(directory, request_id, digest):
    if (str(UUID(request_id)) != request_id or not isinstance(digest, str)
            or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
        raise ValueError("exact receipt binding required")
    source = files("k3_support").joinpath("remote_journal.py").read_text()
    source += '\nimport sys\nprint(json.dumps(read_receipt(*sys.argv[1:]),sort_keys=True))\n'
    return shlex.join(["/usr/bin/python3", "-I", "-S", "-c", source, directory, request_id, digest])


def observe(conn, config, *, request_id, transport=run_process):
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("canonical request required")
    row = conn.execute("SELECT * FROM broker_remote_actions WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        raise ValueError("remote request unavailable")
    plan = json.loads(row["plan_json"])
    directory = receipt_directory(config.raw["runtime"])
    if (not directory or type(plan.get("guard_version")) is not int or plan["guard_version"] != 2
            or plan.get("receipt_directory") != directory or not matches(config, plan)):
        raise ValueError("remote receipt plan unavailable or changed")
    argv = plan_argv(plan, command(directory, request_id, plan.get("command_digest")))
    unknown = {"state": "unknown", "request_id": request_id, "read_only": True, "recovery_authorized": False}
    try:
        output = transport(argv=argv, cwd="/", stdin=b"", heartbeat=lambda: None,
                           env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                           timeout=10, heartbeat_interval=1, output_limit=4096, detailed=True)
        if not isinstance(output, dict) or type(output.get("exit_code")) is not int or output["exit_code"] != 0:
            return unknown
        raw = output["stdout"]
        if not isinstance(raw, str) or len(raw.encode()) > 2048:
            return unknown
        value = json.loads(raw)
        if value == {"state": "unknown"}:
            return unknown
        expected = {"state": "guardian_returned", "version": 1, "request_id": request_id,
                    "command_digest": plan["command_digest"]}
        if (not isinstance(value, dict) or set(value) != {*expected, "guard_exit_code"}
                or any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items())
                or type(value["guard_exit_code"]) is not int or not 0 <= value["guard_exit_code"] <= 255
                or json.dumps(value, sort_keys=True) + "\n" != raw):
            return unknown
    except (ValueError, OSError, KeyError, TypeError, RuntimeError):
        return unknown
    current = conn.execute("SELECT * FROM broker_remote_actions WHERE request_id=?", (request_id,)).fetchone()
    if current is None or dict(current) != dict(row):
        raise ValueError("remote request changed during observation")
    return {**value, "read_only": True, "recovery_authorized": False}
