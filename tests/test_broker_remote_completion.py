from uuid import uuid4

import pytest
from test_broker_completion import add_report
from test_broker_execution_instances import bound

from k3_support.broker_completion import reconcile
from k3_support.broker_dispatch import finish_observed
from k3_support.broker_execution_instances import register
from k3_support.broker_launch_status import snapshot


@pytest.mark.parametrize("state,code,blocked", [
    ("queued", None, False), ("running", None, True), ("unknown", None, True),
    ("succeeded", None, True), ("succeeded", 0, False), ("succeeded", 1, True),
    ("failed", 1, False), ("failed", 124, True), ("failed", 0, True),
    ("cancelled", None, False), ("cancelled", 0, True), ("cancelled", 124, True),
    ("queued", 0, True),
])
def test_remote_work_must_settle_before_worker_completion(conn, config, state, code, blocked):
    args = bound(conn, config)
    register(conn, **args)
    add_report(conn, args["grant_id"])
    conn.execute("INSERT INTO broker_launches VALUES(?,'accepted','fixture','fixture')", (args["claim_request_id"],))
    conn.execute("UPDATE cases SET active_job_id=(SELECT job_id FROM jobs LIMIT 1)")
    grant = conn.execute("SELECT * FROM broker_grants").fetchone()
    target = str(uuid4())
    conn.execute("INSERT INTO broker_remote_actions VALUES(?,?,?,?,?,?,?,?)",
                 (target, grant["worker_uid"], grant["grant_id"], "fixture", "{}", state, "fixture", "fixture"))
    if code is not None:
        conn.execute("INSERT INTO broker_remote_results VALUES(?,?,?,?,?)", (target, code, "", "", "fixture"))
    assert reconcile(conn, grant_id=args["grant_id"])["state"] == "unverified"
    assert conn.execute("SELECT state FROM broker_remote_actions").fetchone()[0] == state
    conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                 (args["grant_id"], args["invocation_id"], 1234, 1, 0, "fixture"))
    result = reconcile(conn, grant_id=args["grant_id"])
    assert result["state"] == ("remote_cleanup_required" if blocked else "review_pending")
    assert finish_observed(conn)["finished"] == (0 if blocked else 1)
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == ("running" if blocked else "succeeded")
    assert bool(conn.execute("SELECT active_job_id FROM cases").fetchone()[0]) is blocked
    if state == "queued":
        assert conn.execute("SELECT state FROM broker_remote_actions").fetchone()[0] == ("cancelled" if code is None else "queued")
    if blocked:
        assert "远端操作" in snapshot(conn)["unresolved"][0]["label"]
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM case_suggestions").fetchone()[0] == (0 if blocked else 1)
