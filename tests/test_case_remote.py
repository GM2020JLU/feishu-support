from uuid import uuid4

import pytest
from test_broker_execution_instances import bound

from k3_support.case_remote import lines


def test_remote_summary_is_scoped_bounded_and_does_not_leak_payloads(conn, config):
    args = bound(conn, config)
    grant = conn.execute("SELECT * FROM broker_grants").fetchone()
    case = conn.execute("SELECT case_id FROM jobs").fetchone()[0]
    for i in range(12):
        target = str(uuid4())
        conn.execute("INSERT INTO broker_remote_actions VALUES(?,?,?,?,?,?,?,?)",
                     (target, grant["worker_uid"], args["grant_id"], "private-digest", '{"command":"private-command"}',
                      "unknown" if i == 0 else "succeeded", str(i).zfill(2), "fixture"))
        if i > 0:
            conn.execute("INSERT INTO broker_remote_results VALUES(?,?,?,?,?)", (target, 0, "private-stdout", "private-stderr", "fixture"))
    before = list(conn.iterdump())
    result = "\n".join(lines(conn, case_id=case))
    assert "共 12 次操作 · 1 次" in result
    assert result.count("请求 ") == 10 and "不会自动重跑" in result
    assert "private-" not in result and grant["token_digest"] not in result
    assert lines(conn, case_id="other-case") == []
    assert list(conn.iterdump()) == before


def test_missing_exit_record_is_not_displayed_as_success(conn, config):
    args = bound(conn, config)
    grant = conn.execute("SELECT * FROM broker_grants").fetchone()
    case = conn.execute("SELECT case_id FROM jobs").fetchone()[0]
    conn.execute("INSERT INTO broker_remote_actions VALUES(?,?,?,?,?,?,?,?)",
                 (str(uuid4()), grant["worker_uid"], args["grant_id"], "fixture", "{}", "succeeded", "fixture", "fixture"))
    result = "\n".join(lines(conn, case_id=case))
    assert "状态与退出记录不一致" in result and "命令退出成功" not in result


def test_case_detail_tracks_remote_status_in_its_snapshot(conn, config):
    from k3_support.case_detail import case_detail
    args = bound(conn, config)
    grant = conn.execute("SELECT * FROM broker_grants").fetchone()
    case = conn.execute("SELECT case_id FROM jobs").fetchone()[0]
    target = str(uuid4())
    conn.execute("INSERT INTO broker_remote_actions VALUES(?,?,?,?,?,?,?,?)",
                 (target, grant["worker_uid"], args["grant_id"], "fixture", "{}", "running", "fixture", "fixture"))
    first = case_detail(conn, case_id=case)["preview"]
    pages = [case_detail(conn, case_id=case, page=p, expected_digest=first["content_digest"])["preview"]["plain_text"]
             for p in range(1, first["page_count"]+1)]
    assert "远端执行（只读" in "\n".join(pages) and target in "\n".join(pages)
    conn.execute("UPDATE broker_remote_actions SET state='unknown' WHERE request_id=?", (target,))
    with pytest.raises(ValueError, match="stale"):
        case_detail(conn, case_id=case, expected_digest=first["content_digest"])
