"""Trusted control-side recovery of quarantined input; never worker RPC."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from .broker_input import project
from .db import transaction
from .ids import digest
from .store import EXECUTABLE_CASE_STATES


def _bound(conn, job_id):
    if not isinstance(job_id, str) or not 1 <= len(job_id) <= 100:
        raise ValueError("任务编号不合法")
    row = conn.execute("""SELECT j.job_id,j.case_id,j.job_type,j.state,j.error_class,
                       j.attempt_no,j.lifecycle_round,j.input_digest,j.updated_at,
                       j.lease_owner,j.lease_expires_at,c.state AS case_state,
                       c.version AS case_version,c.lifecycle_round AS current_round
                       FROM jobs j JOIN cases c USING(case_id) WHERE j.job_id=?""",
                       (job_id,)).fetchone()
    if (row is None or row["job_type"] != "codex" or row["state"] != "waiting"
            or row["error_class"] not in {"broker_input_invalid", "broker_budget_blocked"}
            or row["current_round"] != row["lifecycle_round"]
            or row["case_state"] not in EXECUTABLE_CASE_STATES
            or row["lease_owner"] is not None or row["lease_expires_at"] is not None):
        raise ValueError("当前任务不支持输入恢复，请重新读取")
    try:
        project(conn, job_id=job_id, case_id=row["case_id"],
                lifecycle_round=row["lifecycle_round"], input_digest=row["input_digest"],
                request_id=str(uuid4()))
    except (ValueError, TypeError, RecursionError):
        raise ValueError("输入快照仍缺失或校验失败；需先修复原输入，不能直接重试") from None
    bound = dict(row)
    if row["error_class"] == "broker_budget_blocked":
        if conn.execute("SELECT 1 FROM broker_execution_starts WHERE job_id=? AND attempt_no=?",
                        (job_id, row["attempt_no"])).fetchone():
            raise ValueError("已有启动许可，必须先核对执行结果，不能预算重试")
        policy = conn.execute("SELECT * FROM model_budget_policy WHERE singleton=1").fetchone()
        if policy is None:
            raise ValueError("请先配置预算，不能删除预算策略绕过阻断")
        day = datetime.now(UTC).date().isoformat()
        daily = conn.execute("SELECT coalesce(sum(charged),0) FROM model_budget_attempts WHERE budget_day=?", (day,)).fetchone()[0]
        case_cost = conn.execute("SELECT coalesce(sum(charged),0) FROM model_budget_attempts WHERE case_id=?", (row["case_id"],)).fetchone()[0]
        if daily + policy["attempt_limit"] > policy["daily_limit"] or case_cost + policy["attempt_limit"] > policy["case_limit"]:
            raise ValueError("预算仍不足；先调整额度或核实费用，不要清空占用")
        bound.update(budget_policy=dict(policy), budget_day=day, budget_daily=daily, budget_case=case_cost)
    return bound


def _queue(conn, job_id, now):
    # Old claim UUIDs stay permanently bound to the old attempt. Only a budget
    # denial with no execution permit may release its exact dispatch intent.
    conn.execute("UPDATE broker_launches SET state='finished',updated_at=? WHERE claim_request_id IN "
                 "(SELECT r.request_id FROM broker_claim_receipts r JOIN jobs j "
                 "ON json_extract(r.binding_json,'$.job_id')=j.job_id "
                 "AND json_extract(r.binding_json,'$.execution_round')=j.attempt_no "
                 "WHERE j.job_id=? AND j.error_class='broker_budget_blocked' "
                 "AND NOT EXISTS (SELECT 1 FROM broker_execution_starts s WHERE s.job_id=j.job_id AND s.attempt_no=j.attempt_no))",
                 (now.isoformat(), job_id))
    conn.execute("""UPDATE jobs SET state='queued',error_class=NULL,
                 available_at=?,updated_at=? WHERE job_id=?""",
                 (now.isoformat(), now.isoformat(), job_id))


def preview(conn, *, job_id):
    conn.execute("SAVEPOINT broker_recovery_preview")
    try:
        bound = _bound(conn, job_id)
        return {"job_id": job_id, "case_id": bound["case_id"],
                "binding_digest": digest(bound),
                "message": "原输入及适用预算校验通过。确认后重新排队，可能按当前运行模式自动执行；"
                           "不修改输入、不接管回复、不批准上板或推送。"}
    finally:
        conn.execute("RELEASE broker_recovery_preview")


def apply(conn, *, job_id, binding_digest, request_id, actor_id):
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("缺少控制者身份")
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("操作请求编号不合法")
    if not isinstance(binding_digest, str) or len(binding_digest) != 64:
        raise ValueError("缺少已预览的输入版本")
    with transaction(conn):
        previous = conn.execute("SELECT job_id,binding_digest,actor_id FROM broker_recovery_actions WHERE request_id=?",
                                (request_id,)).fetchone()
        if previous is not None:
            if tuple(previous) != (job_id, binding_digest, actor_id):
                raise ValueError("请求编号已用于其他操作")
            return {"accepted": True, "replayed": True, "execution_authorized": False}
        bound = _bound(conn, job_id)
        if digest(bound) != binding_digest:
            raise ValueError("任务或事项状态已变化，请重新预览")
        now = datetime.now(UTC)
        _queue(conn, job_id, now)
        conn.execute("INSERT INTO broker_recovery_actions VALUES(?,?,?,?,?)",
                     (request_id, job_id, binding_digest, actor_id, now.isoformat()))
        return {"accepted": True, "replayed": False, "execution_authorized": False}


def requeue_input(conn, *, job_id, expected_attempt, expected_lifecycle,
                  expected_digest, expected_updated_at, now=None):
    """Revalidate the original input binding and queue without granting authority.

    Callers must authenticate the operator separately. This does not repair input,
    approve execution, or bypass claim-time runtime policy.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None or type(expected_attempt) is not int or type(expected_lifecycle) is not int:
        raise ValueError("invalid recovery binding")
    with transaction(conn):
        row = _bound(conn, job_id)
        if (row["attempt_no"] != expected_attempt
                or row["lifecycle_round"] != expected_lifecycle
                or row["input_digest"] != expected_digest
                or row["updated_at"] != expected_updated_at):
            raise ValueError("stale recovery binding")
        _queue(conn, job_id, now)
        return {"job_id": job_id, "state": "queued", "execution_authorized": False}
