"""Read-only local evidence for moving beyond an investigation's execution.

Business job state alone is not a process exit. Reuse the broker's committed
resource settlement without invoking its mutating cleanup procedure from a view.
"""

from .broker_remote_state import UNSETTLED
from .project_bugs import BugConflict


def inspect(conn, round_id, *, job_id=None):
    if conn.execute("SELECT 1 FROM project_bug_rounds WHERE round_id=?", (round_id,)).fetchone() is None:
        raise ValueError("investigation round unavailable")
    if job_id is not None and conn.execute("SELECT 1 FROM project_investigation_jobs WHERE round_id=? AND job_id=?", (round_id,job_id)).fetchone() is None:
        raise ValueError("investigation job unavailable")
    blockers = []
    for row in conn.execute(
        """SELECT j.job_id,j.state FROM project_investigation_jobs i JOIN jobs j USING(job_id)
           WHERE i.round_id=? AND (? IS NULL OR i.job_id=?) AND j.state IN ('queued','running','orphaned','unknown')""", (round_id,job_id,job_id),
    ):
        blockers.append({"kind":"job_active", "identity":row["job_id"], "state":row["state"]})
    # A cancelled job may still have an accepted/uncertain systemd launch,
    # including a crash before the worker receives a grant.
    for row in conn.execute(
        """SELECT l.claim_request_id,l.state FROM broker_launch_bindings b
           JOIN broker_launches l USING(claim_request_id)
           JOIN project_investigation_jobs i USING(job_id)
           WHERE i.round_id=? AND (? IS NULL OR i.job_id=?) AND l.state!='finished'
           UNION
           SELECT l.claim_request_id,l.state FROM broker_launches l
           JOIN broker_claim_receipts c ON c.request_id=l.claim_request_id
           JOIN project_investigation_jobs i ON i.job_id=json_extract(c.binding_json,'$.job_id')
           WHERE i.round_id=? AND (? IS NULL OR i.job_id=?) AND l.state!='finished'""", (round_id,job_id,job_id,round_id,job_id,job_id),
    ):
        blockers.append({"kind":"launch_unsettled", "identity":row["claim_request_id"], "state":row["state"]})
    for row in conn.execute(
        """SELECT g.grant_id FROM project_investigation_jobs i JOIN broker_grants g USING(job_id)
           JOIN jobs j ON j.job_id=g.job_id
           JOIN broker_execution_starts s ON s.grant_id=g.grant_id
           LEFT JOIN broker_execution_resources r ON r.grant_id=g.grant_id
           WHERE i.round_id=? AND (? IS NULL OR i.job_id=?) AND (r.settled_at IS NULL OR r.job_id!=g.job_id
             OR r.attempt_no!=g.attempt_no OR r.lifecycle_round!=g.lifecycle_round
             OR r.input_digest!=g.input_digest OR r.case_id!=j.case_id
             OR s.job_id!=g.job_id OR s.attempt_no!=g.attempt_no OR s.peer_uid!=g.worker_uid)""",
        (round_id,job_id,job_id),
    ):
        blockers.append({"kind":"resources_unsettled", "identity":row["grant_id"], "state":"unknown"})
    for row in conn.execute(
        "SELECT a.request_id,a.state FROM project_investigation_jobs i JOIN broker_grants g USING(job_id) "
        "JOIN broker_remote_actions a ON a.grant_id=g.grant_id LEFT JOIN broker_remote_results r USING(request_id) "
        f"WHERE i.round_id=? AND (? IS NULL OR i.job_id=?) AND (a.state='queued' OR {UNSETTLED})", (round_id,job_id,job_id),
    ):
        blockers.append({"kind":"command_unsettled", "identity":row["request_id"], "state":row["state"]})
    from .project_verification_runs import unsettled

    if unsettled(conn, round_id, job_id=job_id):
        blockers.append({"kind":"verification_unsettled", "identity":round_id, "state":"unknown"})
    return {"ready":not blockers, "blockers":blockers[:20], "blocker_count":len(blockers),
            "source":"local_broker_records", "resource_operations_performed":False}


def require(conn, round_id, *, job_id=None):
    status = inspect(conn, round_id, job_id=job_id)
    if not status["ready"]:
        raise BugConflict("investigation resources remain unsettled: " + status["blockers"][0]["kind"])
