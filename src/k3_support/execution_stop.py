"""Exact operator cancellation of one Codex execution, not a communication takeover."""

import json
import uuid

from .db import transaction
from .ids import digest
from .timeutil import iso_now


def _binding(conn, job_id):
    if not isinstance(job_id, str) or len(job_id) > 100:
        raise ValueError("invalid job ID")
    job = conn.execute(
        """SELECT job_id,case_id,job_type,state,attempt_no,lease_owner,lifecycle_round,
                  context_json,pid,process_start_token FROM jobs WHERE job_id=?""",
        (job_id,),
    ).fetchone()
    if (
        job is None
        or job["job_type"] != "codex"
        or job["state"] not in {"queued", "running"}
    ):
        raise ValueError("当前任务不支持此停止入口，请重新读取")
    case = conn.execute(
        "SELECT state,version,lifecycle_round FROM cases WHERE case_id=?",
        (job["case_id"],),
    ).fetchone()
    if case is None or case["lifecycle_round"] != job["lifecycle_round"]:
        raise ValueError("任务执行轮次已变化")
    session = json.loads(job["context_json"]).get("board_session_id")
    lock = None
    if session is not None:
        if not isinstance(session, str) or not session:
            raise ValueError("板卡会话无法核实")
        lock = conn.execute(
            "SELECT lock_key,owner,case_id,metadata_json,acquired_at FROM locks WHERE lock_key='board1'"
        ).fetchone()
        if (
            lock is None
            or lock["owner"] != f"{job['case_id']}:{session}"
            or lock["case_id"] != job["case_id"]
        ):
            raise ValueError("板卡租约已变化，不能请求旧会话收尾")
        if json.loads(lock["metadata_json"]).get("session_id") != session:
            raise ValueError("板卡会话不匹配")
        sibling = conn.execute(
            "SELECT 1 FROM jobs WHERE job_id<>? AND case_id=? AND state IN ('queued','running','waiting','orphaned') AND json_extract(context_json,'$.board_session_id')=?",
            (job_id, job["case_id"], session),
        ).fetchone()
        if sibling:
            raise ValueError("板卡会话关联多个未结束任务，需要核对执行占用")
    return {
        "job": dict(job),
        "case": dict(case),
        "lock": dict(lock) if lock else None,
        "session": session,
    }


def preview(conn, *, job_id):
    conn.execute("SAVEPOINT execution_stop_preview")
    try:
        bound = _binding(conn, job_id)
        return {
            "job_id": job_id,
            "case_id": bound["job"]["case_id"],
            "state": bound["job"]["state"],
            "binding_digest": digest(bound),
            "board_session_id": bound["session"],
            "message": "停止本次 Codex 执行，不接管回复、不批准推送。请求被接受不代表进程已退出；有板卡会话时还需核验 BROM 收尾和租约释放。",
        }
    finally:
        conn.execute("RELEASE execution_stop_preview")


def apply(conn, *, job_id, binding_digest, request_id, actor_id):
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("缺少控制者身份")
    if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
        raise ValueError("invalid request ID")
    if not isinstance(binding_digest, str):
        raise TypeError("缺少执行版本")
    with transaction(conn):
        previous = conn.execute(
            "SELECT job_id,binding_digest,actor_id FROM execution_stop_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if previous:
            if tuple(previous) != (job_id, binding_digest, actor_id):
                raise ValueError("请求编号已用于其他操作")
            return {
                "accepted": True,
                "replayed": True,
                "process_exit_verified": False,
                "board_cleanup_verified": False,
            }
        bound = _binding(conn, job_id)
        if digest(bound) != binding_digest:
            raise ValueError("执行状态已变化，请重新预览")
        job = bound["job"]
        now = iso_now()
        conn.execute(
            "INSERT INTO execution_stop_requests(request_id,job_id,binding_digest,actor_id,previous_state,board_session_id,requested_at,target_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                request_id,
                job_id,
                binding_digest,
                actor_id,
                job["state"],
                bound["session"],
                now,
                json.dumps(
                    {
                        key: job[key]
                        for key in (
                            "case_id",
                            "attempt_no",
                            "lifecycle_round",
                            "lease_owner",
                            "pid",
                            "process_start_token",
                        )
                    }
                ),
            ),
        )
        conn.execute(
            "UPDATE jobs SET state='cancelled',error_class='operator_execution_stop',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?",
            (now, job_id),
        )
        # Running jobs use the worker's finally cleanup after process termination.
        # A queued job has no worker to run that finally; reconcile its exact lease.
        if bound["session"] and job["state"] == "queued":
            conn.execute(
                "UPDATE locks SET expires_at=?,heartbeat_at=? WHERE lock_key='board1' AND owner=? AND case_id=?",
                (now, now, bound["lock"]["owner"], job["case_id"]),
            )
    return {
        "accepted": True,
        "replayed": False,
        "process_exit_verified": False,
        "board_cleanup_verified": False,
    }


def status(conn, *, job_id):
    conn.execute("SAVEPOINT execution_stop_status")
    try:
        row = conn.execute(
            "SELECT * FROM execution_stop_requests WHERE job_id=? ORDER BY requested_at DESC,request_id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            return {"requested": False}
        target = json.loads(row["target_json"]) if row["target_json"] else None
        process = "not_started" if row["previous_state"] == "queued" else "unverified"
        if target and process == "unverified":
            receipts = conn.execute(
                "SELECT pid,process_start_token FROM execution_exit_receipts WHERE job_id=? AND attempt_no=? AND lease_owner IS ?",
                (job_id, target["attempt_no"], target["lease_owner"]),
            ).fetchall()
            if any(
                (target["pid"] is None or target["pid"] == r["pid"])
                and (
                    target["process_start_token"] is None
                    or target["process_start_token"] == r["process_start_token"]
                )
                and r["process_start_token"] != "unavailable"
                for r in receipts
            ):
                process = "main_process_exited"
        cleanup = "not_applicable" if row["board_session_id"] is None else "unverified"
        service_process = "not_applicable"
        if target:
            grant = conn.execute(
                "SELECT grant_id FROM broker_grants WHERE job_id=? AND attempt_no=? "
                "AND lease_owner IS ? AND lifecycle_round=?",
                (job_id, target["attempt_no"], target["lease_owner"], target.get("lifecycle_round")),
            ).fetchone()
            if grant:
                service_process = "unverified"
                evidence = conn.execute(
                    "SELECT 1 FROM broker_service_exits e JOIN broker_execution_instances i "
                    "ON i.grant_id=e.grant_id AND i.invocation_id=e.invocation_id WHERE e.grant_id=?",
                    (grant["grant_id"],),
                ).fetchone()
                if evidence:
                    service_process = "service_main_exited"
        if target and row["board_session_id"]:
            from .review import ReviewError, _verified_board_cleanup

            try:
                _verified_board_cleanup(
                    conn,
                    case_id=target["case_id"],
                    session_id=row["board_session_id"],
                    require_unoccupied=False,
                )
                cleanup = "verified"
            except (ReviewError, ValueError, TypeError, KeyError):
                pass
        return {
            "requested": True,
            "requested_at": row["requested_at"],
            "process": process,
            "service_process": service_process,
            "board_cleanup": cleanup,
            "descendant_isolation_verified": False,
        }
    finally:
        conn.execute("RELEASE execution_stop_status")
