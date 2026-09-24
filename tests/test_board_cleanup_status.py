import pytest
from test_broker_execution_instances import bound

from k3_support.board_cleanup_status import items, lines
from k3_support.execution_inventory import page


@pytest.mark.parametrize("state", ["running", "unknown", "succeeded"])
def test_cleanup_status_is_scoped_readonly_and_not_physical_proof(conn, config, state):
    args = bound(conn, config)
    grant = args["grant_id"]
    row = conn.execute("SELECT job_id,case_id FROM jobs").fetchone()
    conn.execute("INSERT INTO broker_board_cleanup VALUES(?,?,?,?,?)", (grant, "board-session", state, "fixture", "fixture"))
    before = list(conn.iterdump())
    result = items(conn, job_id=row["job_id"])
    assert result[0]["state"] == state and len(result) == 1
    assert items(conn, case_id="other-case") == []
    assert "不代表板卡当前空闲" in "\n".join(lines(conn, case_id=row["case_id"]))
    inventory = page(conn, config)
    assert inventory["items"][0]["board_cleanup"] == result
    assert inventory["board"]["physical_state"] == "unknown"
    assert not any(key in str(result) for key in ("lease_token", "token_digest", "context_json"))
    assert list(conn.iterdump()) == before
    from k3_support.case_content_inventory import preview
    inventory = preview(conn, row['case_id'])
    assert any(h['reason']=='board_cleanup_unsettled' for h in inventory['observed_holds']) == (state!='succeeded')
    group = inventory['canonical_group_holds']['members']
    assert any(h['reason']=='board_cleanup_unsettled' for member in group for h in member['observed_holds']) == (state!='succeeded')
    assert list(conn.iterdump()) == before


def test_case_detail_cleanup_change_invalidates_old_preview(conn, config):
    from k3_support.case_detail import case_detail
    args = bound(conn, config)
    case = conn.execute("SELECT case_id FROM jobs").fetchone()[0]
    conn.execute("INSERT INTO broker_board_cleanup VALUES(?,?,?,?,?)", (args["grant_id"], "board-session", "running", "fixture", "fixture"))
    initial = case_detail(conn, case_id=case)["preview"]
    conn.execute("UPDATE broker_board_cleanup SET state='unknown'")
    with pytest.raises(ValueError, match="stale"):
        case_detail(conn, case_id=case, expected_digest=initial["content_digest"])


def test_failed_jobs_cannot_hide_unresolved_board_cleanup(conn, config):
    args = bound(conn, config)
    conn.execute("UPDATE jobs SET state='failed'")
    conn.execute("INSERT INTO broker_board_cleanup VALUES(?,?,?,?,?)", (args["grant_id"], "board-session", "unknown", "fixture", "fixture"))
    before = list(conn.iterdump())
    result = page(conn, config, state="active")
    assert result["items"] == []
    assert result["board"]["cleanup_pending"]["total"] == 1
    assert result["board"]["cleanup_pending"]["items"][0]["session_id"] == "board-session"
    assert list(conn.iterdump()) == before
