"""One result source for recording and review; broker reports never fall back to files."""

import hashlib
from pathlib import Path


def read_result(conn, *, job_id):
    job = conn.execute("""SELECT j.*,c.lifecycle_round AS case_round
                          FROM jobs j JOIN cases c USING(case_id) WHERE j.job_id=?""",
                       (job_id,)).fetchone()
    if job is None or job["job_type"] != "codex":
        raise ValueError("Codex job unavailable")
    # Existence of any broker grant makes this a broker job, even before a result
    # arrives. Never silently read a worker-controlled file for that job.
    broker = conn.execute("SELECT 1 FROM broker_grants WHERE job_id=?", (job_id,)).fetchone()
    if not broker:
        return (Path(job["workdir"]) / "codex-final.md").read_bytes()
    report = conn.execute("""SELECT r.*,g.job_id AS grant_job,g.attempt_no AS grant_attempt,
                             g.lifecycle_round AS grant_round,g.input_digest AS grant_input
                             FROM broker_results r JOIN broker_grants g USING(grant_id)
                             WHERE r.job_id=? AND r.attempt_no=?""",
                          (job_id, job["attempt_no"])).fetchone()
    if (report is None or job["lifecycle_round"] != job["case_round"]
            or report["lifecycle_round"] != job["lifecycle_round"]
            or report["input_digest"] != job["input_digest"]
            or report["grant_job"] != job_id or report["grant_attempt"] != job["attempt_no"]
            or report["grant_round"] != job["lifecycle_round"]
            or report["grant_input"] != job["input_digest"]):
        raise ValueError("broker result binding unavailable")
    raw = report["result_text"].encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != report["result_digest"]:
        raise ValueError("broker result digest mismatch")
    return raw
