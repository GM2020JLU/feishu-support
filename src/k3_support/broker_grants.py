"""Control-side grant operations. Never expose these functions as worker RPCs."""

import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta

from .db import transaction


def issue(conn, *, job_id, attempt_no, lease_owner, worker_uid, now=None, _token=None):
    """Trusted control-only issuance, inside the task-claim transaction."""
    from .store import EXECUTABLE_CASE_STATES

    if not conn.in_transaction:
        raise ValueError("control transaction required")
    if type(worker_uid) is not int or not 0 < worker_uid < 4294967295 or worker_uid == os.geteuid():
        raise ValueError("independent worker UID required")
    if not isinstance(job_id, str) or not job_id or type(attempt_no) is not int or attempt_no < 1:
        raise ValueError("invalid job binding")
    if not isinstance(lease_owner, str) or not lease_owner:
        raise ValueError("lease owner required")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    row = conn.execute("""SELECT j.*,c.state AS case_state,c.lifecycle_round AS case_round
                          FROM jobs j JOIN cases c USING(case_id) WHERE j.job_id=?""", (job_id,)).fetchone()
    if (not row or row["job_type"] != "codex" or row["state"] != "running"
            or row["case_state"] not in EXECUTABLE_CASE_STATES or row["attempt_no"] != attempt_no
            or row["lease_owner"] != lease_owner or row["lifecycle_round"] != row["case_round"]):
        raise ValueError("job no longer eligible")
    try:
        lease_end = datetime.fromisoformat(row["lease_expires_at"])
    except (ValueError, TypeError) as error:
        raise ValueError("job lease unavailable") from error
    if lease_end.tzinfo is None or lease_end <= now:
        raise ValueError("job lease expired")
    if conn.execute("SELECT 1 FROM broker_grants WHERE job_id=? AND attempt_no=?", (job_id, attempt_no)).fetchone():
        raise ValueError("attempt already has a grant")
    if _token is not None and (not isinstance(_token, str) or len(_token) != 64
                               or any(ch not in "0123456789abcdef" for ch in _token)):
        raise ValueError("invalid control-derived token")
    token = _token if _token is not None else secrets.token_urlsafe(32)
    grant_id = "grant_" + secrets.token_hex(16)
    expiry = min(lease_end, now + timedelta(minutes=5)).isoformat()
    conn.execute("INSERT INTO broker_grants VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                 (grant_id, hashlib.sha256(token.encode()).hexdigest(), worker_uid, job_id, attempt_no,
                  row["lifecycle_round"], row["input_digest"], lease_owner, now.isoformat(), expiry))
    return {"grant_id": grant_id, "token": token, "expires_at": expiry}


def verify_bound_task(conn, params, *, peer_uid, now=None):
    """For decoded requests inside a control transaction; does not execute an action."""
    from .store import EXECUTABLE_CASE_STATES

    if not conn.in_transaction:
        raise ValueError("control transaction required")
    now = now or datetime.now(UTC)
    binding = verify(conn, token=params["lease_token"], peer_uid=peer_uid, now=now)
    if any(binding[left] != params[right] for left, right in (
        ("job_id", "job_id"), ("attempt_no", "execution_round"),
        ("lifecycle_round", "lifecycle_round"), ("input_digest", "input_digest"),
    )):
        raise ValueError("grant task binding changed")
    row = conn.execute("""SELECT j.*,c.state AS case_state,c.lifecycle_round AS case_round
                          FROM jobs j JOIN cases c USING(case_id)
                          WHERE j.job_id=? AND j.case_id=?""", (binding["job_id"], params["case_id"])).fetchone()
    if (not row or row["job_type"] != "codex" or row["state"] != "running"
            or row["case_state"] not in EXECUTABLE_CASE_STATES
            or any(row[key] != binding[key] for key in ("attempt_no", "lifecycle_round", "input_digest", "lease_owner"))
            or row["case_round"] != binding["lifecycle_round"]):
        raise ValueError("live task binding changed")
    try:
        expiry = datetime.fromisoformat(row["lease_expires_at"])
    except (ValueError, TypeError) as error:
        raise ValueError("task lease unavailable") from error
    if expiry.tzinfo is None or expiry <= now:
        raise ValueError("task lease expired")
    return binding


def verify(conn, *, token, peer_uid, now=None):
    """Read grant binding only. peer_uid must come from authenticated kernel credentials."""
    if type(peer_uid) is not int or not 0 < peer_uid < 4294967295:
        raise ValueError("invalid peer UID")
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise ValueError("grant unavailable")
    try:
        token_digest = hashlib.sha256(token.encode()).hexdigest()
    except UnicodeError as error:
        raise ValueError("grant unavailable") from error
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    row = conn.execute("SELECT * FROM broker_grants WHERE token_digest=? AND worker_uid=?",
                       (token_digest, peer_uid)).fetchone()
    if row is None or row["revoked_at"] is not None:
        raise ValueError("grant unavailable")
    try:
        created = datetime.fromisoformat(row["created_at"])
        expires = datetime.fromisoformat(row["expires_at"])
    except (ValueError, TypeError) as error:
        raise ValueError("grant validity unavailable") from error
    if created.tzinfo is None or expires.tzinfo is None or not created <= now < expires:
        raise ValueError("grant outside validity")
    return {key: row[key] for key in ("grant_id", "job_id", "attempt_no", "lifecycle_round", "input_digest", "lease_owner")}


def revoke(conn, *, grant_id, now=None):
    if not isinstance(grant_id, str) or not grant_id or len(grant_id) > 256:
        raise ValueError("invalid grant ID")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    with transaction(conn):
        row = conn.execute("SELECT grant_id,revoked_at FROM broker_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if row is None:
            raise ValueError("grant unavailable")
        if row["revoked_at"] is None:
            conn.execute("UPDATE broker_grants SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL",
                         (now.isoformat(), grant_id))
        result = dict(conn.execute("SELECT grant_id,revoked_at FROM broker_grants WHERE grant_id=?", (grant_id,)).fetchone())
    return {**result, "process_exit_verified": False, "board_cleanup_verified": False}
