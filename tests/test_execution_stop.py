import uuid

import pytest
from test_coding_budget import setup

from k3_support.execution_stop import apply, preview, status


def payload(conn, job):
    return {
        "job_id": job,
        "binding_digest": preview(conn, job_id=job)["binding_digest"],
        "request_id": str(uuid.uuid4()),
        "actor_id": "owner",
    }


def test_cancel_one_job_without_case_or_communication_changes(conn, config):
    _, job = setup(conn, config)
    cases = [tuple(r) for r in conn.execute("SELECT * FROM cases")]
    args = payload(conn, job)
    result = apply(conn, **args)
    assert (
        result["accepted"]
        and not result["process_exit_verified"]
        and not result["board_cleanup_verified"]
    )
    assert (
        conn.execute("SELECT state FROM jobs WHERE job_id=?", (job,)).fetchone()[0]
        == "cancelled"
    )
    assert cases == [tuple(r) for r in conn.execute("SELECT * FROM cases")]
    assert apply(conn, **args)["replayed"]
    assert status(conn, job_id=job)["process"] == "not_started"
    assert (
        conn.execute("SELECT count(*) FROM execution_stop_requests").fetchone()[0] == 1
    )
    with pytest.raises(ValueError):
        apply(conn, **{**args, "actor_id": "other"})


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE jobs SET state='running'",
        "UPDATE jobs SET attempt_no=attempt_no+1",
        "UPDATE cases SET version=version+1",
    ],
)
def test_changed_execution_rejects_old_confirmation(conn, config, mutation):
    _, job = setup(conn, config)
    args = payload(conn, job)
    conn.execute(mutation)
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        apply(conn, **args)
    assert list(conn.iterdump()) == before


def test_running_stop_keeps_process_identity_for_recovery(conn, config):
    _, job = setup(conn, config)
    conn.execute(
        "UPDATE jobs SET state='running',lease_owner='worker',pid=123,process_start_token='original'"
    )
    apply(conn, **payload(conn, job))
    row = conn.execute("SELECT pid,process_start_token,state FROM jobs").fetchone()
    assert tuple(row) == (123, "original", "cancelled")
    assert status(conn, job_id=job)["process"] == "unverified"
    conn.execute(
        "INSERT INTO execution_exit_receipts VALUES('exit_wrong',?,99,'worker',123,'original',-15,'now')",
        (job,),
    )
    assert status(conn, job_id=job)["process"] == "unverified"
    attempt = conn.execute(
        "SELECT attempt_no FROM jobs WHERE job_id=?", (job,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO execution_exit_receipts VALUES('exit_right',?,?,'worker',123,'original',-15,'now')",
        (job, attempt),
    )
    result = status(conn, job_id=job)
    assert result["process"] == "main_process_exited"
    assert result["board_cleanup"] == "not_applicable"
    assert not result["descendant_isolation_verified"]


def test_foreign_board_lease_cannot_be_stopped(conn, config):
    _, job = setup(conn, config)
    conn.execute(
        "UPDATE jobs SET context_json=json_set(context_json,'$.board_session_id','session')"
    )
    conn.execute(
        "INSERT INTO locks VALUES('board1','someone-else',NULL,'board','now','later','now','{}')"
    )
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="租约已变化"):
        preview(conn, job_id=job)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("state", ["queued", "running"])
def test_board_cleanup_is_deferred_until_running_worker_exits(conn, config, state):
    _, job = setup(conn, config)
    case = conn.execute("SELECT case_id FROM jobs WHERE job_id=?", (job,)).fetchone()[0]
    conn.execute(
        "UPDATE jobs SET state=?,context_json=json_set(context_json,'$.board_session_id','session')",
        (state,),
    )
    conn.execute(
        "INSERT INTO locks VALUES('board1',?,?,'board','start','2099-01-01T00:00:00+00:00','heartbeat','{\"session_id\":\"session\"}')",
        (f"{case}:session", case),
    )
    apply(conn, **payload(conn, job))
    lock = conn.execute("SELECT * FROM locks").fetchone()
    assert lock is not None
    assert (lock["expires_at"] == "2099-01-01T00:00:00+00:00") == (state == "running")
    assert (
        conn.execute("SELECT board_session_id FROM execution_stop_requests").fetchone()[
            0
        ]
        == "session"
    )
