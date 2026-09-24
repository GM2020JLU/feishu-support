"""Atomic failure recording fenced to the worker's observed execution attempt."""
from .timeutil import iso_now


def fail_attempt(conn, *, job_id, attempt_no, error_class):
    if type(attempt_no) is not int or attempt_no < 0:
        raise ValueError('invalid attempt number')
    if not isinstance(error_class, str) or not 1 <= len(error_class) <= 100:
        raise ValueError('invalid error class')
    changed = conn.execute(
        """UPDATE jobs SET state='failed',error_class=?,lease_owner=NULL,
           lease_expires_at=NULL,updated_at=?
           WHERE job_id=? AND attempt_no=? AND state IN ('running','succeeded')
             AND NOT EXISTS (SELECT 1 FROM case_events WHERE idempotency_key=?)
             AND EXISTS (SELECT 1 FROM cases WHERE cases.case_id=jobs.case_id
                         AND cases.lifecycle_round=jobs.lifecycle_round)""",
        (error_class, iso_now(), job_id, attempt_no, f'job:{job_id}:recorded'),
    ).rowcount
    return bool(changed)
