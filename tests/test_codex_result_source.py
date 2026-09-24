import pytest
from test_broker_results import NOW, result_request

from k3_support.broker_results import submit
from k3_support.codex_result_source import read_result
from k3_support.executors import record_codex_result
from k3_support.review import ReviewError, _reviewed_result_sections


def test_broker_report_records_without_worker_file(conn):
    request = result_request(conn)
    submit(conn, request, peer_uid=1234, now=NOW)
    assert read_result(conn, job_id="job-1") == request["params"]["result"].encode()
    # This is a synthetic control-side completion, not process-exit evidence.
    conn.execute("UPDATE jobs SET state='succeeded'")
    assert record_codex_result(conn, job_id="job-1")["status"] == "completed"
    assert conn.execute("SELECT count(*) FROM case_suggestions").fetchone()[0] == 1


@pytest.mark.parametrize("change", [
    "DELETE FROM broker_results",
    "UPDATE broker_results SET result_text='tampered'",
    "UPDATE jobs SET attempt_no=2",
    "UPDATE cases SET lifecycle_round=lifecycle_round+1",
    "UPDATE jobs SET input_digest='different'",
    "UPDATE broker_grants SET input_digest='different'",
])
def test_no_file_fallback_for_missing_or_stale_broker_report(conn, change):
    request = result_request(conn)
    submit(conn, request, peer_uid=1234, now=NOW)
    conn.execute(change)
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        read_result(conn, job_id="job-1")
    assert list(conn.iterdump()) == before


def test_board_continuation_uses_reviewed_broker_report(conn):
    request = result_request(conn)
    submit(conn, request, peer_uid=1234, now=NOW)
    digest = conn.execute("SELECT result_digest FROM broker_results").fetchone()[0]
    before = list(conn.iterdump())
    assert _reviewed_result_sections(conn, job_id="job-1", expected_digest=digest)["status"] == "completed"
    with pytest.raises(ReviewError, match="changed"):
        _reviewed_result_sections(conn, job_id="job-1", expected_digest="0" * 64)
    assert list(conn.iterdump()) == before
