"""Control-only atomic Codex assignment; not yet exposed as replayable worker RPC."""

import os
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .broker_grants import issue
from .broker_policy import execution_allowed
from .broker_queue import next_candidate
from .db import transaction


def claim_next(conn, config, *, worker_uid, now=None, _token=None, contract=None, job_id=None):
    if type(worker_uid) is not int or not 0 < worker_uid < 4294967295 or worker_uid == os.geteuid():
        raise ValueError("independent worker UID required")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    with nullcontext(conn) if conn.in_transaction else transaction(conn):
        if not execution_allowed(conn, config, contract=contract):
            return None
        row = next_candidate(conn, now=now, contract=contract, job_id=job_id)
        if row is None:
            return None
        owner = "broker:" + str(uuid4())
        attempt = row["attempt_no"] + 1
        expiry = (now + timedelta(seconds=120)).isoformat()
        conn.execute("""UPDATE jobs SET state='running',lease_owner=?,lease_expires_at=?,
                     heartbeat_at=?,attempt_no=?,updated_at=? WHERE job_id=?""",
                     (owner, expiry, now.isoformat(), attempt, now.isoformat(), row["job_id"]))
        grant = issue(conn, job_id=row["job_id"], attempt_no=attempt, lease_owner=owner,
                      worker_uid=worker_uid, now=now, _token=_token)
        return {"job_id": row["job_id"], "case_id": row["case_id"],
                "execution_round": attempt, "lifecycle_round": row["lifecycle_round"],
                "input_digest": row["input_digest"], "lease_token": grant["token"]}
