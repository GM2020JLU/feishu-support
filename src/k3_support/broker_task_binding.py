"""Read-only live task fence; neither peer authentication nor capability issuance."""

from datetime import UTC, datetime

from .job_capability import verified_context


class BindingError(ValueError):
    pass


def verify_live_task(conn, params, *, now=None):
    from .store import EXECUTABLE_CASE_STATES

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise BindingError("timezone required")
    row = conn.execute(
        """SELECT j.*,c.state AS case_state,c.lifecycle_round AS case_round
           FROM jobs j JOIN cases c USING(case_id) WHERE j.job_id=? AND j.case_id=?""",
        (params["job_id"], params["case_id"]),
    ).fetchone()
    if not row or row["job_type"] != "codex" or row["state"] != "running" or row["case_state"] not in EXECUTABLE_CASE_STATES:
        raise BindingError("task not executable")
    if (row["attempt_no"] != params["execution_round"]
            or row["lifecycle_round"] != params["lifecycle_round"]
            or row["case_round"] != params["lifecycle_round"]
            or row["input_digest"] != params["input_digest"] or not row["lease_owner"]):
        raise BindingError("task binding changed")
    try:
        expiry = datetime.fromisoformat(row["lease_expires_at"])
    except (TypeError, ValueError) as error:
        raise BindingError("lease unavailable") from error
    if expiry.tzinfo is None or expiry <= now:
        raise BindingError("lease expired or ambiguous")
    verified_context(row["context_json"], params["lease_token"])
    return {"job_id": row["job_id"], "case_id": row["case_id"], "attempt_no": row["attempt_no"],
            "lifecycle_round": row["lifecycle_round"], "lease_owner": row["lease_owner"]}
