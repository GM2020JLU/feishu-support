"""Control-owned session accounting, not a provider-enforced spending cap."""

from .ids import digest
from .model_budget import BudgetError, _dispatch, _reserve


def block_unstarted(conn, *, params, peer_uid, now=None):
    """After a rolled-back budget failure, park only the still-current task."""
    from .broker_grants import verify_bound_task
    from .budget_blocks import record
    from .db import transaction
    from .timeutil import iso_now

    with transaction(conn):
        binding = verify_bound_task(conn, params, peer_uid=peer_uid, now=now)
        if conn.execute("SELECT 1 FROM broker_execution_starts WHERE job_id=? AND attempt_no=?",
                        (binding["job_id"], binding["attempt_no"])).fetchone():
            return False
        conn.execute("UPDATE jobs SET state='waiting',error_class='broker_budget_blocked',"
                     "lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?",
                     (iso_now(), binding["job_id"]))
        record(conn, binding["job_id"], "broker_start", "budget_gate_blocked")
    return True


def charge_start(conn, *, binding, case_id, contract, now):
    if not conn.in_transaction:
        raise BudgetError("start transaction required")
    policy = conn.execute("SELECT * FROM model_budget_policy WHERE singleton=1").fetchone()
    if policy is None:
        return
    if contract is None:
        raise BudgetError("trusted contract required")
    attempt = _reserve(conn, request_id="broker:" + binding["grant_id"], case_id=case_id,
                       provider=contract.provider, model=contract.model, amount=policy["attempt_limit"],
                       input_digest=digest({"task": binding["input_digest"], "contract": contract.fingerprint}), at=now)
    if not attempt["created"] or not _dispatch(conn, attempt["attempt_id"]):
        raise BudgetError("budget start already used or denied")
    conn.execute("INSERT INTO broker_budget_attempts VALUES(?,?)", (binding["grant_id"], attempt["attempt_id"]))
    from .budget_blocks import resolve

    resolve(conn, binding["job_id"], "broker_start")


def validate_running(conn, *, grant_id, contract):
    from .broker_resources import require_open

    require_open(conn, grant_id=grant_id)
    if conn.execute("SELECT 1 FROM broker_service_exits WHERE grant_id=?", (grant_id,)).fetchone():
        raise ValueError("service already exited")
    row = conn.execute("SELECT c.contract_digest,a.state,a.policy_revision FROM broker_execution_contracts c "
                       "LEFT JOIN broker_budget_attempts b USING(grant_id) "
                       "LEFT JOIN model_budget_attempts a USING(attempt_id) WHERE c.grant_id=?", (grant_id,)).fetchone()
    if row is None or contract is None or row["contract_digest"] != contract.fingerprint:
        raise ValueError("running contract changed")
    if row["state"] is not None and row["state"] != "dispatched":
        raise ValueError("budget execution no longer active")
    policy = conn.execute("SELECT revision FROM model_budget_policy WHERE singleton=1").fetchone()
    if policy and (row["state"] != "dispatched" or row["policy_revision"] != policy["revision"]):
        raise ValueError("running budget authorization changed")
