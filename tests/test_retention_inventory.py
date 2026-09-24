import pytest

from k3_support.retention_inventory import page
from k3_support.store import ingest_event


def test_retention_metadata_pagination_preserves_files_and_private_fields(conn):
    for i, state in enumerate(("quarantined", "failed", "restored")):
        event, _ = ingest_event(conn, source="feishu_user_poll", identity="user", external_id=f"retention-{i}",
                                payload={"secret": "private-body"}, occurred_at="2026-01-01T00:00:00+00:00")
        conn.execute("INSERT INTO retention_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (f"attempt-{i}", event, "/private/original", f"/private/quarantine-{i}", "{}", "fixture", state, "private-error", "fixture", "fixture"))
    before = list(conn.iterdump())
    first = page(conn, limit=1)
    assert first["total_matching"] == 3 and first["next_cursor"] == "attempt-0"
    assert len(page(conn, after_id=first["next_cursor"])["items"]) == 2
    assert page(conn, state="failed")["items"][0]["state"] == "failed"
    assert "private" not in str(page(conn))
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("args", [{"state": []}, {"state": "unknown"}, {"limit": True}, {"after_id": None}])
def test_bad_inventory_filter_rejected(conn, args):
    with pytest.raises(ValueError):
        page(conn, **args)
