"""Reconcile observed broker execution, not repair success or permission to reply."""

import hashlib
import json

from .broker_resources import capture, inspect_settlement
from .codex_result_source import read_result
from .db import transaction
from .executors import ExecutorError, record_codex_result, validate_codex_result_text
from .store import EXECUTABLE_CASE_STATES
from .timeutil import iso_now


def _clear_case_execution(conn, row, context):
    # Only detach this exact execution. A newer task/session must stay visible.
    conn.execute(
        "UPDATE cases SET active_job_id=NULL,active_session_id=CASE WHEN active_session_id IS ? "
        "THEN NULL ELSE active_session_id END,version=version+1,updated_at=? "
        "WHERE case_id=? AND lifecycle_round=? AND active_job_id=?",
        (context.get("board_session_id"), iso_now(), row["case_id"], row["grant_round"], row["job_id"]),
    )


def reconcile(conn, *, grant_id):
    with transaction(conn):
        row = conn.execute("""SELECT j.*,g.attempt_no AS grant_attempt,g.lifecycle_round AS grant_round,
            g.input_digest AS grant_input,g.lease_owner AS grant_owner,c.state AS case_state,
            c.lifecycle_round AS case_round,e.exit_code AS service_exit_code,e.exit_status AS service_exit_status,
            s.authorized_at AS start_authorized_at
            FROM broker_grants g JOIN jobs j ON j.job_id=g.job_id JOIN cases c ON c.case_id=j.case_id
            JOIN broker_execution_instances i ON i.grant_id=g.grant_id
            JOIN broker_execution_starts s ON s.grant_id=g.grant_id
            JOIN broker_service_exits e ON e.grant_id=i.grant_id AND e.invocation_id=i.invocation_id
            WHERE g.grant_id=?""", (grant_id,)).fetchone()
        if row is None:
            return {"state": "unverified"}
        # Manager exit is not a billing receipt: retain the full reservation.
        conn.execute("UPDATE model_budget_attempts SET state='unknown',updated_at=? "
                     "WHERE state='dispatched' AND attempt_id IN "
                     "(SELECT attempt_id FROM broker_budget_attempts WHERE grant_id=?)", (iso_now(), grant_id))
        settlement = inspect_settlement(conn, grant_id=grant_id)
        if settlement.state != 'settled':
            return {"state": settlement.reason, "repair_verified": False, "reply_sent": False}
        # Direct broker workers have no dispatcher launch row. Their exact
        # service exit and resource checks still authorize resource settlement;
        # business acceptance below remains a separate decision.
        conn.execute('UPDATE broker_execution_resources SET settled_at=? WHERE grant_id=? AND settled_at IS NULL',
                     (iso_now(), grant_id))
        if (row["attempt_no"] != row["grant_attempt"] or row["lifecycle_round"] != row["grant_round"]
                or row["input_digest"] != row["grant_input"] or row["case_round"] != row["grant_round"]
                or row["case_state"] not in EXECUTABLE_CASE_STATES):
            return {"state": "stale"}
        context = json.loads(row["context_json"])
        resource = capture(conn, grant_id=grant_id)
        if context.get('board_session_id') != resource['board_session_id']:
            # Current business context still must agree. Resource release itself
            # uses the immutable snapshot, not this potentially newer context.
            return {"state": "board_cleanup_required", "repair_verified": False, "reply_sent": False}
        if row["state"] not in {"running", "succeeded"}:
            return {"state": "not_applicable"}
        if row["state"] == "running" and row["lease_owner"] != row["grant_owner"]:
            return {"state": "stale"}
        if row["service_exit_code"] != 1 or row["service_exit_status"] != 0:
            if row["state"] == "running":
                exit_code = row["service_exit_status"] if row["service_exit_code"] == 1 else -row["service_exit_status"]
                conn.execute(
                    "UPDATE jobs SET state='failed',exit_code=?,error_class='broker_service_failed',"
                    "output_digest=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=? AND state='running'",
                    (exit_code, iso_now(), row["job_id"]),
                )
                _clear_case_execution(conn, row, context)
            return {"state": "execution_failed"}
        try:
            raw = read_result(conn, job_id=row["job_id"])
            validate_codex_result_text(raw.decode("utf-8"))
        except (ValueError, ExecutorError):
            conn.execute(
                "UPDATE jobs SET state='waiting',error_class='broker_report_unavailable',"
                "lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=? AND state='running'",
                (iso_now(), row["job_id"]),
            )
            _clear_case_execution(conn, row, context)
            return {"state": "report_review_required"}
        conn.execute("UPDATE jobs SET state='succeeded',exit_code=0,error_class=NULL,output_digest=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                     "WHERE job_id=? AND state='running'", (hashlib.sha256(raw).hexdigest(), iso_now(), row["job_id"]))
        _clear_case_execution(conn, row, context)
    # Existing recorder produces a shadow suggestion, not an external reply.
    # Replays repair a crash between job reconciliation and suggestion recording.
    record_codex_result(conn, job_id=row["job_id"])
    return {"state": "review_pending", "repair_verified": False, "reply_sent": False}
