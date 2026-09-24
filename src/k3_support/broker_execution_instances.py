"""Control-only binding of an authorized attempt to a supervisor-observed unit.

No worker RPC uses this module. Observation arguments must come from the trusted
service manager collector, not messages, worker output or environment variables.
Registration is not exit evidence and never changes job or board state.
"""

import json
import re
from pathlib import PurePosixPath
from uuid import UUID

from .db import transaction
from .timeutil import iso_now


def register(conn, *, grant_id, claim_request_id, invocation_id, cgroup_path):
    if not isinstance(claim_request_id, str) or str(UUID(claim_request_id)) != claim_request_id:
        raise ValueError("canonical claim identity required")
    if not isinstance(invocation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", invocation_id) or invocation_id == "0" * 32:
        raise ValueError("service manager invocation identity required")
    unit = f"k3-support-broker-worker@{claim_request_id}.service"
    if (not isinstance(cgroup_path, str) or len(cgroup_path) > 4096
            or not cgroup_path.startswith("/") or "\x00" in cgroup_path
            or any(part in {".", "..", ""} for part in cgroup_path.split("/")[1:])
            or PurePosixPath(cgroup_path).name != unit):
        raise ValueError("exact worker unit cgroup required")
    with transaction(conn):
        start = conn.execute("SELECT * FROM broker_execution_starts WHERE grant_id=?", (grant_id,)).fetchone()
        if start is None:
            raise ValueError("authorized execution required")
        claim = conn.execute("SELECT binding_json FROM broker_claim_receipts WHERE peer_uid=? AND request_id=?",
                             (start["peer_uid"], claim_request_id)).fetchone()
        binding = json.loads(claim[0]) if claim else None
        grant = conn.execute("SELECT * FROM broker_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if (not isinstance(binding, dict) or grant is None
                or any(binding.get(key) != grant[column] for key, column in (
                    ("job_id", "job_id"), ("execution_round", "attempt_no"),
                    ("lifecycle_round", "lifecycle_round"), ("input_digest", "input_digest")))):
            raise ValueError("claim does not identify this execution")
        job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (grant["job_id"],)).fetchone()
        if not job or binding.get("case_id") != job["case_id"]:
            raise ValueError("execution Case changed")
        values = (grant_id, claim_request_id, unit, invocation_id, cgroup_path)
        old = conn.execute("SELECT * FROM broker_execution_instances WHERE grant_id=?", (grant_id,)).fetchone()
        if old:
            if tuple(old)[:5] != values:
                raise ValueError("execution instance already bound")
            return {"registered": True, "replayed": True}
        conn.execute("INSERT INTO broker_execution_instances VALUES(?,?,?,?,?,?)", (*values, iso_now()))
        return {"registered": True, "replayed": False}
