"""Policy-gated heartbeat; started tasks have a bounded control-owned lifetime."""

from datetime import UTC, datetime, timedelta

from .broker_budget import validate_running
from .broker_grants import verify_bound_task
from .broker_policy import execution_allowed
from .broker_receipts import execute


def renew(conn, request, *, peer_uid, now=None, config=None, contract_reader=None):
    now = now or datetime.now(UTC)
    contract = contract_reader() if contract_reader else None
    if request.get("method") != "renew":
        raise ValueError("renew request required")

    def handler(db, binding, validated):
        # execute() checked the live task under the same write transaction.
        job = db.execute("SELECT lease_expires_at FROM jobs WHERE job_id=?",
                         (binding["job_id"],)).fetchone()
        lease_end = datetime.fromisoformat(job["lease_expires_at"])
        start = db.execute("SELECT authorized_at FROM broker_execution_starts WHERE grant_id=?",
                           (binding["grant_id"],)).fetchone()
        if config is not None and start is not None:
            if contract_reader is not None:
                validate_running(db, grant_id=binding["grant_id"], contract=contract)
            started = datetime.fromisoformat(start["authorized_at"])
            # Match the existing Codex executor's 7200-second total timeout.
            deadline = started + timedelta(seconds=7200)
            if started.tzinfo is None or not started <= now < deadline:
                raise ValueError("execution lifetime exhausted")
            lease_end = min(now + timedelta(seconds=120), deadline)
            db.execute("UPDATE jobs SET lease_expires_at=?,heartbeat_at=?,updated_at=? WHERE job_id=?",
                       (lease_end.isoformat(), now.isoformat(), now.isoformat(), binding["job_id"]))
        expiry = min(lease_end, now + timedelta(minutes=5)).isoformat()
        db.execute("UPDATE broker_grants SET expires_at=? WHERE grant_id=?",
                   (expiry, binding["grant_id"]))
        return {"accepted": True, "job_id": binding["job_id"], "expires_at": expiry}

    def allowed(db):
        if not execution_allowed(db, config, contract=contract):
            return False
        if contract_reader is not None:
            binding = verify_bound_task(db, request["params"], peer_uid=peer_uid, now=now)
            if db.execute("SELECT 1 FROM broker_execution_starts WHERE grant_id=?", (binding["grant_id"],)).fetchone():
                validate_running(db, grant_id=binding["grant_id"], contract=contract)
        return True
    gate = None if config is None else allowed
    return execute(conn, request, peer_uid=peer_uid, handler=handler, now=now, allow_operation=gate)
