import pytest

from k3_support.db import connect
from k3_support.incidents import diagnostic_context_records, record_diagnostic_snapshot
from k3_support.store import create_case


@pytest.mark.parametrize("change_source", [False, True])
def test_late_diagnostic_result_observes_other_connection(conn, config, change_source):
    case_id, _ = create_case(
        conn, title="synthetic", case_type="bug", severity="P2", confidence=0.8
    )
    conn.execute(
        """INSERT INTO inbound_events(event_pk,source,identity,external_id,idempotency_key,
               occurred_at,occurred_epoch,received_at,received_epoch,payload_json,status)
           VALUES('race_diag','feishu_user_poll','user','race_diag','race_diag',
                  '2026-09-03T00:00:00+00:00',1,'2026-09-03T00:00:00+00:00',1,'{}','processed')"""
    )
    first = {"facts": {"boot_media": "UFS"}, "missing": [], "confidence": 0.8}

    def slow_result(value):
        other = connect(config.database_path)
        try:
            if change_source:
                other.execute(
                    "UPDATE inbound_events SET payload_json=? WHERE event_pk='race_diag'",
                    ('{"edited":true}',),
                )
            else:
                record_diagnostic_snapshot(
                    other,
                    case_id=case_id,
                    event_pk="race_diag",
                    content="synthetic",
                    extractor=lambda _: first,
                )
        finally:
            other.close()
        return {"facts": {"boot_media": "NVMe"}, "missing": [], "confidence": 0.99}

    result = record_diagnostic_snapshot(
        conn,
        case_id=case_id,
        event_pk="race_diag",
        content="synthetic",
        extractor=slow_result,
    )
    if change_source:
        assert result is None
        assert (
            conn.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0
        )
        assert diagnostic_context_records(conn, case_id=case_id) == []
    else:
        assert result["facts"] == first["facts"]
        assert result["confidence"] == first["confidence"]
        assert (
            conn.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 1
        )
        assert (
            diagnostic_context_records(conn, case_id=case_id)[0]["facts"]
            == first["facts"]
        )
