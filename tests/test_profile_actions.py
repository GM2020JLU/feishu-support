import uuid

import pytest

from k3_support.profile_actions import apply
from k3_support.profile_inventory import page
from k3_support.routing import set_requester_profile


def request(conn):
    set_requester_profile(
        conn,
        requester_id="person",
        relationship="unknown",
        function_role="unknown",
        display_name="同事",
    )
    return {
        "requester_id": "person",
        "content_digest": page(conn)["items"][0]["content_digest"],
        "relationship": "supervisor",
        "function_role": "engineering",
        "reason": "本人核实",
        "actor_id": "owner",
        "request_id": str(uuid.uuid4()),
    }


def test_profile_correction_is_atomic_and_replay_does_not_reapply(conn):
    payload = request(conn)
    assert not apply(conn, **payload)["replayed"]
    row = conn.execute("SELECT * FROM requester_profiles").fetchone()
    assert row["relationship"] == "supervisor" and row["source"] == "operator"
    assert row["display_name"] == "同事"
    conn.execute("UPDATE requester_profiles SET relationship='peer'")
    assert apply(conn, **payload)["replayed"]
    assert (
        conn.execute("SELECT relationship FROM requester_profiles").fetchone()[0]
        == "peer"
    )
    assert conn.execute("SELECT count(*) FROM profile_actions").fetchone()[0] == 1


def test_profile_changed_since_view_rejected_without_audit(conn):
    payload = request(conn)
    conn.execute("UPDATE requester_profiles SET department='new'")
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="已变化"):
        apply(conn, **payload)
    assert list(conn.iterdump()) == before


def test_reused_request_cannot_change_target_decision(conn):
    payload = request(conn)
    apply(conn, **payload)
    with pytest.raises(ValueError, match="其他修改"):
        apply(conn, **{**payload, "relationship": "peer"})


def test_reset_clears_override_without_directory_lookup(conn):
    from k3_support.audit_inventory import page as audit_page

    payload = request(conn)
    apply(conn, **payload)
    item = page(conn)["items"][0]
    reset = {
        **payload,
        "request_id": str(uuid.uuid4()),
        "content_digest": item["content_digest"],
        "relationship": "unknown",
        "function_role": "unknown",
        "reset_auto": True,
    }
    apply(conn, **reset)
    row = conn.execute("SELECT * FROM requester_profiles").fetchone()
    assert row["source"] == "unknown" and row["relationship"] == "unknown"
    assert row["relationship_confidence"] == row["function_confidence"] == 0
    assert row["verified_at"] is None and row["expires_at"] is None
    assert row["department"] is None and row["job_title"] is None
    records = audit_page(conn, kind="profiles")
    assert records["total_matching"] == 2
    assert "本人核实" not in str(records)
    assert apply(conn, **reset)["replayed"]
