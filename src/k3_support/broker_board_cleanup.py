"""One-shot control-side cleanup after independently observed worker exit."""

import json
import os
from datetime import datetime
from pathlib import Path

from .board_operation_guard import BoardOperationBusy
from .board_serial_observer import endpoint
from .broker_resources import cancel_unstarted, exited_binding, require_open
from .broker_process import run_process
from .db import transaction
from .executors import BoardExecutor, ExecutionResult
from .timeutil import iso_now


def _binding(conn, grant_id, *, require_lock=True):
    if require_lock:
        require_open(conn, grant_id=grant_id)
    row = exited_binding(conn, grant_id=grant_id)
    session = row['board_session_id']
    if not session:
        raise ValueError("cleanup session binding unavailable")
    lock = conn.execute("SELECT owner,case_id FROM locks WHERE lock_key='board1'").fetchone()
    if require_lock and (lock is None or lock["case_id"] != row["case_id"] or lock["owner"] != f"{row['case_id']}:{session}"):
        raise ValueError("cleanup does not own board")
    return row["case_id"], session


def _recover(conn, intent):
    """Recover only an already completed exact attempt; never perform device I/O."""
    from .review import ReviewError, _verified_board_cleanup

    try:
        case, session = _binding(conn, intent["grant_id"], require_lock=False)
        if session != intent["session_id"]:
            return False
        checked = _verified_board_cleanup(conn, case_id=case, session_id=session, require_unoccupied=False)
        created = datetime.fromisoformat(intent["created_at"])
        if created.tzinfo is None:
            return False
        for item in checked:
            ended = datetime.fromisoformat(item["finished_at"])
            if (item["cleanup_attempt_id"] != "broker_"+intent["grant_id"]
                    or ended.tzinfo is None or ended < created):
                return False
    except (ValueError, TypeError, KeyError, ReviewError):
        return False
    conn.execute("UPDATE broker_board_cleanup SET state='succeeded',updated_at=? WHERE grant_id=? AND state IN ('running','unknown')",
                 (iso_now(), intent["grant_id"]))
    return True


def run_one(conn, config, *, contract_reader, stop_event=None, transport=run_process):
    if endpoint(config.raw["runtime"]) is None:
        return {"state": "cleanup_not_configured"}
    if stop_event is not None and stop_event.is_set():
        return {"state": "stopped"}
    with transaction(conn):
        # Restart never repeats a cleanup whose side effects are uncertain.
        unresolved = conn.execute("SELECT * FROM broker_board_cleanup WHERE state IN ('running','unknown') ORDER BY created_at,grant_id LIMIT 1").fetchone()
        if unresolved is not None:
            if _recover(conn, unresolved):
                return {"state": "board_cleanup_recovered", "grant_id": unresolved["grant_id"],
                        "repair_verified": False, "reply_sent": False}
            return {"state": "cleanup_unknown"}
        if conn.execute("SELECT 1 FROM broker_board_actions WHERE state IN ('running','unknown')").fetchone():
            return {"state": "occupied"}
        candidates = conn.execute("""SELECT g.grant_id FROM broker_grants g
            JOIN broker_execution_instances i USING(grant_id)
            JOIN broker_service_exits e ON e.grant_id=i.grant_id AND e.invocation_id=i.invocation_id
            LEFT JOIN broker_board_cleanup b USING(grant_id) WHERE b.grant_id IS NULL
            ORDER BY g.created_at,g.grant_id""").fetchall()
        chosen = None
        for candidate in candidates:
            try:
                binding = _binding(conn, candidate["grant_id"])
            except (ValueError, TypeError):
                continue
            chosen = candidate["grant_id"]
            break
        if chosen is None:
            return {"state": "idle"}
        fingerprint = contract_reader().fingerprint
        contract = conn.execute("SELECT contract_digest FROM broker_execution_contracts WHERE grant_id=?", (chosen,)).fetchone()
        if contract is None or contract[0] != fingerprint:
            return {"state": "cleanup_contract_changed"}
        # Worker exit makes queued commands obsolete, but not in-flight commands.
        cancel_unstarted(conn, grant_id=chosen)
        conn.execute("INSERT INTO broker_board_cleanup VALUES(?,?,'running',?,?)", (chosen, binding[1], iso_now(), iso_now()))

    def heartbeat():
        if stop_event is not None and stop_event.is_set():
            raise ValueError("cleanup stopped")
        with transaction(conn):
            intent = conn.execute("SELECT state,session_id FROM broker_board_cleanup WHERE grant_id=?", (chosen,)).fetchone()
            if intent is None or tuple(intent) != ("running", binding[1]):
                raise ValueError("cleanup intent changed")
            if _binding(conn, chosen) != binding or contract_reader().fingerprint != fingerprint:
                raise ValueError("cleanup binding changed")

    def runner(argv, cwd, timeout):
        value = transport(argv=argv, cwd=cwd or "/", stdin=b"", heartbeat=heartbeat,
                          heartbeat_interval=1, timeout=timeout, output_limit=128000, detailed=True,
                          env={"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"})
        if (not isinstance(value, dict) or type(value.get("exit_code")) is not int
                or not -255 <= value["exit_code"] <= 255
                or any(not isinstance(value.get(k), str) for k in ("stdout", "stderr"))
                or len((value["stdout"]+value["stderr"]).encode()) > 128000):
            raise ValueError("invalid cleanup process receipt")
        return ExecutionResult(argv, value["exit_code"], value["stdout"], value["stderr"])

    try:
        BoardExecutor(config, runner=runner).close_session(conn, case_id=binding[0], session_id=binding[1], authorize=heartbeat,
                                                         cleanup_attempt_id="broker_"+chosen)
    except BoardOperationBusy:
        with transaction(conn):
            conn.execute("DELETE FROM broker_board_cleanup WHERE grant_id=? AND state='running'", (chosen,))
        return {"state": "busy"}  # Lock denied before any device operation.
    except Exception:
        with transaction(conn):
            conn.execute("UPDATE broker_board_cleanup SET state='unknown',updated_at=? WHERE grant_id=?", (iso_now(), chosen))
        raise
    with transaction(conn):
        conn.execute("UPDATE broker_board_cleanup SET state='succeeded',updated_at=? WHERE grant_id=?", (iso_now(), chosen))
    return {"state": "board_cleaned", "grant_id": chosen, "repair_verified": False, "reply_sent": False}
