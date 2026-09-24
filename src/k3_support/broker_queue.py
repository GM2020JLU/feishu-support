"""Shared control-side eligibility for dispatch and atomic assignment."""

from uuid import uuid4

from .broker_input import project
from .store import EXECUTABLE_CASE_STATES


def next_candidate(conn, *, now, contract=None, job_id=None, require_selection=False):
    """Validate a bounded queue window under the caller's transaction.

    Invalid immutable inputs are parked without starting a worker or consuming an
    attempt. Other deployments' tasks remain queued and do not consume the window.
    """
    if not conn.in_transaction:
        raise ValueError("queue selection requires a transaction")
    states = ",".join("?" for _ in EXECUTABLE_CASE_STATES)
    # Filter before LIMIT so another tool's queued tasks cannot starve this
    # deployment. project() below still verifies the immutable input digest.
    selection_sql = "json_type(b.payload_json,'$.context_extra.execution') IS NULL"
    selection_args = ()
    if contract is not None:
        selection_sql = """json_extract(b.payload_json,'$.model')=?
            AND json_extract(b.payload_json,'$.reasoning')=? AND (
              (json_type(b.payload_json,'$.context_extra.execution') IS NULL AND ?='codex')
              OR (json_extract(b.payload_json,'$.context_extra.execution.agent')=?
                  AND json_extract(b.payload_json,'$.context_extra.execution.contract_fingerprint')=?))"""
        selection_args = (contract.model, contract.reasoning, contract.agent, contract.agent, contract.fingerprint)
    if require_selection:
        selection_sql += " AND json_type(b.payload_json,'$.context_extra.execution')='object'"
    job_sql = "AND j.job_id=?" if job_id is not None else ""
    job_args = (job_id,) if job_id is not None else ()
    candidates = conn.execute(
        f"""SELECT j.* FROM jobs j JOIN cases c USING(case_id) JOIN broker_inputs b USING(job_id)
            WHERE j.job_type='codex' AND j.state='queued' AND j.available_at<=?
            AND j.lifecycle_round=c.lifecycle_round AND c.state IN ({states})
            AND ({selection_sql}) {job_sql}
            ORDER BY j.priority,j.created_at,j.job_id LIMIT 32""",
        (now.isoformat(), *EXECUTABLE_CASE_STATES, *selection_args, *job_args),
    ).fetchall()
    row = None
    for candidate in candidates:
        try:
            project(conn, job_id=candidate["job_id"], case_id=candidate["case_id"],
                    lifecycle_round=candidate["lifecycle_round"], input_digest=candidate["input_digest"],
                    request_id=str(uuid4()))
        except ValueError:
            # Fixed public reason only; never persist input text or exceptions.
            conn.execute("""UPDATE jobs SET state='waiting',error_class='broker_input_invalid',
                         lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?""",
                         (now.isoformat(), candidate["job_id"]))
            continue
        row = candidate
        break
    return row
