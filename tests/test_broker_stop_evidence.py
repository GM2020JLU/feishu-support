from uuid import uuid4

from test_broker_execution_instances import bound

from k3_support.broker_execution_instances import register
from k3_support.execution_stop import apply, preview, status


def test_service_exit_display_is_bound_to_exact_stopped_attempt(conn, config):
    args = bound(conn, config)
    register(conn, **args)
    current = preview(conn, job_id="job-1")
    apply(conn, job_id="job-1", binding_digest=current["binding_digest"],
          request_id=str(uuid4()), actor_id="synthetic-controller")
    assert status(conn, job_id="job-1")["service_process"] == "unverified"
    conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                 (args["grant_id"], args["invocation_id"], 1234, 2, 15, "synthetic-time"))
    observed = status(conn, job_id="job-1")
    assert observed["service_process"] == "service_main_exited"
    assert observed["process"] == "unverified"
    assert not observed["descendant_isolation_verified"]
    assert observed["board_cleanup"] == "not_applicable"
    conn.execute("UPDATE execution_stop_requests SET target_json=json_set(target_json,'$.attempt_no',99)")
    assert status(conn, job_id="job-1")["service_process"] == "not_applicable"
