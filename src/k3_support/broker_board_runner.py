"""Control-side board consumer; no implicit occupancy approval or BROM claim."""

import json
import os
from pathlib import Path

from .board_operation_guard import BoardOperationBusy
from .broker_board import authorize_queued
from .broker_process import run_process
from .db import transaction
from .executors import (
    BoardExecutor,
    ExecutionResult,
    ExecutorError,
    validate_board_action,
)
from .timeutil import iso_now


def run_one(conn, config, *, contract_reader, transport=run_process, stop_event=None):
    with transaction(conn):
        if stop_event is not None and stop_event.is_set():
            return {"state": "stopped"}
        if conn.execute("SELECT 1 FROM broker_board_actions WHERE state IN ('running','unknown')").fetchone():
            return {"state": "occupied"}
        action = conn.execute("SELECT * FROM broker_board_actions WHERE state='queued' ORDER BY created_at,request_id LIMIT 1").fetchone()
        if action is None:
            return {"state": "idle"}
        try:
            case_id = authorize_queued(conn, config, action, contract=contract_reader())
            operation = validate_board_action(json.loads(action["action_json"]))
        except (ValueError, TypeError, KeyError, ExecutorError):
            conn.execute("UPDATE broker_board_actions SET state='cancelled',updated_at=? WHERE request_id=?", (iso_now(), action["request_id"]))
            return {"state": "cancelled"}
        conn.execute("UPDATE broker_board_actions SET state='running',updated_at=? WHERE request_id=?", (iso_now(), action["request_id"]))

    def heartbeat():
        if stop_event is not None and stop_event.is_set():
            raise ValueError("board consumer stopping")
        with transaction(conn):
            authorize_queued(conn, config, action, contract=contract_reader())

    captured = None
    def run(argv, cwd, timeout):
        nonlocal captured
        value = transport(argv=argv, cwd=cwd or "/", stdin=b"", heartbeat=heartbeat, timeout=timeout,
                          heartbeat_interval=1, output_limit=128000, detailed=True,
                          env={"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"})
        if (not isinstance(value, dict) or type(value.get("exit_code")) is not int or not -255 <= value["exit_code"] <= 255
                or any(not isinstance(value.get(k), str) for k in ("stdout", "stderr"))
                or len((value["stdout"]+value["stderr"]).encode()) > 128000):
            raise ValueError("invalid board process output")
        captured = ExecutionResult(argv, value["exit_code"], value["stdout"], value["stderr"])
        if operation["type"] == "serial_wait" and captured.returncode == 0:
            from .board_serial_evidence import fresh_match

            fresh_match(captured.stdout, operation["regex"])
        return captured

    state = "unknown"
    try:
        try:
            result = BoardExecutor(config, runner=run).execute(conn, case_id=case_id, session_id=action["session_id"],
                action=operation, authorize=heartbeat, operation_id=action["request_id"])
        except ExecutorError:
            if captured is None or captured.returncode == 0:
                raise
            result = captured
        with transaction(conn):
            authorize_queued(conn, config, action, contract=contract_reader())
            state = "unknown" if result.returncode < 0 or result.returncode in (124, 125, 255) else ("succeeded" if result.returncode == 0 else "failed")
            conn.execute("INSERT INTO broker_board_results VALUES(?,?,?,?,?)",
                         (action["request_id"], result.returncode, result.stdout, result.stderr, iso_now()))
            conn.execute("UPDATE broker_board_actions SET state=?,updated_at=? WHERE request_id=?", (state, iso_now(), action["request_id"]))
    except BoardOperationBusy:
        # The exclusive lock refused before device I/O; preserve the same intent.
        with transaction(conn):
            conn.execute("UPDATE broker_board_actions SET state='queued',updated_at=? WHERE request_id=? AND state='running'",
                         (iso_now(), action["request_id"]))
        return {"state": "busy", "request_id": action["request_id"]}
    finally:
        with transaction(conn):
            conn.execute("UPDATE broker_board_actions SET state='unknown',updated_at=? WHERE request_id=? AND state IN ('running','cancelled')",
                         (iso_now(), action["request_id"]))
    return {"state": state, "request_id": action["request_id"], "board_cleanup_verified": False}
