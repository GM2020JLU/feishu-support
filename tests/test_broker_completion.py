import hashlib

import pytest
from test_broker_execution_instances import bound

from k3_support.approvals import request_approval
from k3_support.broker_completion import reconcile
from k3_support.broker_execution_instances import register
from k3_support.executors import CODEX_RESULT_SECTIONS
from k3_support.ids import canonical_json, digest


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "no_exit"])
def test_service_exit_never_refunds_unverified_model_cost(conn, config, outcome):
    from test_model_budget import policy

    from k3_support import model_budget

    args = bound(conn, config)
    register(conn, **args)
    policy(conn)
    case_id = conn.execute("SELECT case_id FROM jobs").fetchone()[0]
    attempt = model_budget.reserve(conn, request_id="synthetic-broker-cost", case_id=case_id,
                                   provider="fixture", model="gpt-5.6-sol", amount=60,
                                   input_digest=digest("synthetic session"))
    assert model_budget.dispatch(conn, attempt["attempt_id"])
    conn.execute("INSERT INTO broker_budget_attempts VALUES(?,?)", (args["grant_id"], attempt["attempt_id"]))
    if outcome != "no_exit":
        conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                     (args["grant_id"], args["invocation_id"], 1234, 1,
                      int(outcome == "failure"), "synthetic"))
    if outcome == "success":
        add_report(conn, args["grant_id"])
    if outcome == "cancelled":
        conn.execute("UPDATE jobs SET state='cancelled'")
    reconcile(conn, grant_id=args["grant_id"])
    resource = conn.execute('SELECT settled_at FROM broker_execution_resources WHERE grant_id=?',
                            (args['grant_id'],)).fetchone()
    if outcome != 'no_exit':
        assert resource['settled_at'] is not None
    else:
        assert resource is None or resource['settled_at'] is None
    row = conn.execute("SELECT state,charged,reserved FROM model_budget_attempts").fetchone()
    assert tuple(row) == ("dispatched" if outcome == "no_exit" else "unknown", 60, 60)
    before = list(conn.iterdump())
    reconcile(conn, grant_id=args["grant_id"])
    assert list(conn.iterdump()) == before


def add_report(conn, grant_id):
    grant = conn.execute("SELECT * FROM broker_grants WHERE grant_id=?", (grant_id,)).fetchone()
    sections = {name: "completed" if name == "status" else "not verified" for name in CODEX_RESULT_SECTIONS}
    text = "\n".join(f"## {name}\n{value}" for name, value in sections.items())
    conn.execute("INSERT INTO broker_results VALUES(?,?,?,?,?,?,?,?,?)",
                 (grant_id, grant["job_id"], grant["attempt_no"], grant["lifecycle_round"], grant["input_digest"],
                  hashlib.sha256(text.encode()).hexdigest(), text, canonical_json(sections), "synthetic"))


@pytest.mark.parametrize("state", ["running", "unknown", "succeeded"])
def test_unconfirmed_cleanup_intent_blocks_review_even_without_job_session(conn, config, state):
    args = bound(conn, config)
    register(conn, **args)
    add_report(conn, args["grant_id"])
    conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                 (args["grant_id"], args["invocation_id"], 1234, 1, 0, "fixture"))
    conn.execute("INSERT INTO broker_board_cleanup VALUES(?,?,?,?,?)",
                 (args["grant_id"], "bound-board-session", state, "fixture", "fixture"))
    result = reconcile(conn, grant_id=args["grant_id"])
    assert result["state"] == "board_cleanup_required" and not result["reply_sent"]
    assert conn.execute('SELECT settled_at FROM broker_execution_resources WHERE grant_id=?',
                        (args['grant_id'],)).fetchone()[0] is None
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "running"
    assert conn.execute("SELECT count(*) FROM case_suggestions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("current", [True, False])
def test_finished_execution_does_not_clear_a_new_case_owner(conn, config, current):
    args = bound(conn, config)
    register(conn, **args)
    add_report(conn, args["grant_id"])
    conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                 (args["grant_id"], args["invocation_id"], 1234, 1, 0, "synthetic"))
    owner = "job-1" if current else "new-job"
    conn.execute("UPDATE cases SET active_job_id=?,active_session_id='other-session'", (owner,))
    assert reconcile(conn, grant_id=args["grant_id"])["state"] == "review_pending"
    row = conn.execute("SELECT active_job_id,active_session_id FROM cases").fetchone()
    assert tuple(row) == (None if current else "new-job", "other-session")


@pytest.mark.parametrize("mutation", [None, "cancelled", "board", "wrong_round", "missing_report", "corrupt_report"])
def test_completion_stages_review_without_reply_or_repair_claim(conn, config, mutation):
    args = bound(conn, config)
    register(conn, **args)
    conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                 (args["grant_id"], args["invocation_id"], 1234, 1, 0, "synthetic"))
    if mutation != "missing_report":
        add_report(conn, args["grant_id"])
    if mutation == "cancelled":
        conn.execute("UPDATE jobs SET state='cancelled'")
    if mutation == "board":
        conn.execute("UPDATE jobs SET context_json=json_set(context_json,'$.board_session_id','synthetic-board')")
    if mutation == "wrong_round":
        conn.execute("UPDATE jobs SET attempt_no=attempt_no+1")
    if mutation == "corrupt_report":
        conn.execute("UPDATE broker_results SET result_text='corrupted synthetic report'")
    if mutation in {"missing_report", "corrupt_report"}:
        assert reconcile(conn, grant_id=args["grant_id"])["state"] == "report_review_required"
        assert tuple(conn.execute("SELECT state,error_class FROM jobs").fetchone()) == ("waiting", "broker_report_unavailable")
        before = list(conn.iterdump())
        assert reconcile(conn, grant_id=args["grant_id"])["state"] == "not_applicable"
        assert list(conn.iterdump()) == before
    else:
        result = reconcile(conn, grant_id=args["grant_id"])
        assert result["state"] == {None: "review_pending", "cancelled": "not_applicable",
                                   "board": "board_cleanup_required", "wrong_round": "stale"}[mutation]
    if mutation is None:
        assert not result["repair_verified"] and not result["reply_sent"]
        assert reconcile(conn, grant_id=args["grant_id"])["state"] == "review_pending"
        assert conn.execute("SELECT count(*) FROM case_suggestions").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "succeeded"
    else:
        assert conn.execute("SELECT count(*) FROM case_suggestions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("old", [False, True])
@pytest.mark.parametrize("operation", [None, "before", "after", "missing_receipt", "wrong_session", "bad_time", "cancelled", "lost_context"])
def test_board_completion_requires_same_session_cleanup_after_execution_start(conn, config, old, operation):
    args = bound(conn, config, board_session="synthetic-board-session")
    register(conn, **args)
    case = conn.execute("SELECT case_id FROM jobs").fetchone()[0]
    session = "synthetic-board-session"
    conn.execute("UPDATE jobs SET context_json=json_set(context_json,'$.board_session_id',?)", (session,))
    approval, _, _ = request_approval(conn, approval_type="board1_lease", case_id=case,
                                      action={"synthetic": True}, session_id=session,
                                      expires_at="2099-01-01T00:00:00+00:00")
    conn.execute("UPDATE approvals SET status='consumed',consumed_at='2026-09-08T00:00:00+00:00' WHERE approval_id=?", (approval,))
    stamp = "2025-01-01T00:00:00+00:00" if old else "2026-09-08T00:01:00+00:00"
    actions = [{"type": "enter_brom"}, {"type": "serial_wait",
               "regex": "usb_init : enter|usb_core_init : enter|ROM: usb download handler", "timeout": 45}]
    for action in actions:
        key = f"{case}:board1:{session}:{digest(action)}:cleanup:synthetic-attempt"
        conn.execute("INSERT INTO action_ledger(action_key,action_type,case_id,state,input_digest,result_json,finished_at,created_at,updated_at) VALUES(?,?,?,'verified',?,?,?,?,?)",
                     (key, action["type"], case, digest(action), canonical_json({"returncode": 0, "stdout": "synthetic"}), stamp, stamp, stamp))
    conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                 (args["grant_id"], args["invocation_id"], 1234, 1, 0, "synthetic"))
    add_report(conn, args["grant_id"])
    if operation:
        from uuid import uuid4
        ident = str(uuid4())
        grant = conn.execute("SELECT * FROM broker_grants WHERE grant_id=?", (args["grant_id"],)).fetchone()
        ended = "2026-09-08T00:00:30+00:00"
        if operation == "after":
            ended = "2026-09-08T00:02:00+00:00"
        elif operation == "bad_time":
            ended = "2026-09-08T00:00:30"
        conn.execute("INSERT INTO broker_board_actions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (ident, grant["worker_uid"], args["grant_id"],
                      "other-session" if operation == "wrong_session" else session,
                      "fixture", '{"type":"reset"}', "fixture", "fixture",
                      "cancelled" if operation == "cancelled" else "succeeded", ended, ended))
        if operation not in ("missing_receipt", "cancelled"):
            conn.execute("INSERT INTO broker_board_results VALUES(?,?,?,?,?)", (ident, 0, "synthetic", "", ended))
        if operation == "lost_context":
            conn.execute("UPDATE jobs SET context_json=json_remove(context_json,'$.board_session_id')")
    result = reconcile(conn, grant_id=args["grant_id"])
    blocked = old or operation not in (None, "before", "cancelled")
    assert result["state"] == ("board_cleanup_required" if blocked else "review_pending")
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM locks").fetchone()[0] == 0


@pytest.mark.parametrize("code,status,expected", [(1, 1, 1), (2, 15, -15), (3, 11, -11)])
def test_confirmed_failed_exit_ends_execution_without_success_draft(conn, config, code, status, expected):
    args = bound(conn, config)
    register(conn, **args)
    conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                 (args["grant_id"], args["invocation_id"], 1234, code, status, "synthetic"))
    assert reconcile(conn, grant_id=args["grant_id"])["state"] == "execution_failed"
    row = conn.execute("SELECT state,exit_code,error_class,lease_owner FROM jobs").fetchone()
    assert tuple(row) == ("failed", expected, "broker_service_failed", None)
    before = list(conn.iterdump())
    assert reconcile(conn, grant_id=args["grant_id"])["state"] == "not_applicable"
    assert list(conn.iterdump()) == before
    assert conn.execute("SELECT count(*) FROM case_suggestions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
