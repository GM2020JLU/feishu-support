import json

from test_broker_results import NOW, result_request

from k3_support.broker_results import submit
from k3_support.case_detail import case_detail


def test_case_detail_distinguishes_report_from_verified_completion(conn):
    request = result_request(conn)
    case_id = request["params"]["case_id"]
    assert "代理报告已接收" not in case_detail(conn, case_id=case_id)["preview"]["text"]
    submit(conn, request, peer_uid=1234, now=NOW)
    before = list(conn.iterdump())
    text = case_detail(conn, case_id=case_id)["preview"]["text"]
    assert "代理报告已接收，等待执行退出和结果核验" in text
    assert "不代表问题已解决或板卡已释放" in text
    assert "broker-secret" not in text
    assert list(conn.iterdump()) == before
    conn.execute("UPDATE jobs SET attempt_no=2")
    assert "代理报告已接收" not in case_detail(conn, case_id=case_id)["preview"]["text"]


def test_case_detail_names_selected_coding_tool_instead_of_internal_job_type(conn):
    request = result_request(conn)
    case_id = request["params"]["case_id"]
    conn.execute("UPDATE jobs SET context_json=? WHERE job_id='job-1'",
                 (json.dumps({"agent": "opencode"}),))
    detail = case_detail(conn, case_id=case_id)["preview"]["text"]
    assert "任务 OpenCode · job-1：" in detail
    assert "任务 codex：" not in detail
