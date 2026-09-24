import pytest

from k3_support.profile_inventory import page
from k3_support.routing import set_requester_profile


@pytest.mark.parametrize(
    "source,expiry,status,role",
    [
        ("operator", None, "operator_override", "qa"),
        ("operator", "2000-01-01T00:00:00+00:00", "operator_override", "qa"),
        ("feishu_contact", None, "untrusted", "unknown"),
        ("feishu_contact", "broken", "untrusted", "unknown"),
        ("feishu_contact", "2999-01-01T00:00:00", "untrusted", "unknown"),
        ("feishu_contact", "2000-01-01T00:00:00+00:00", "untrusted", "unknown"),
        ("feishu_contact", "2999-01-01T00:00:00+00:00", "valid_cache", "qa"),
    ],
)
def test_inventory_distinguishes_cached_and_effective_roles(
    conn, source, expiry, status, role
):
    from k3_support.routing import audience_strategy, get_requester_profile

    set_requester_profile(
        conn,
        requester_id="p",
        relationship="peer",
        function_role="qa",
        source=source,
        expires_at=expiry,
    )
    before = list(conn.iterdump())
    item = page(conn)["items"][0]
    assert item["function_role"] == "qa"
    assert item["effective_profile"]["function_role"] == role
    assert item["authority_status"] == status
    assert item["effective_profile"] == get_requester_profile(conn, "p")
    assert item["effective_strategy"] == audience_strategy(item["effective_profile"])
    if status == "untrusted":
        assert not item["effective_strategy"]["auto_clarify"]
    assert list(conn.iterdump()) == before


def test_profiles_paginate_filter_and_hide_raw_evidence(conn):
    for i in range(4):
        set_requester_profile(
            conn,
            requester_id=f"person-{i}",
            relationship="peer",
            function_role="qa",
            display_name="测试同学",
            evidence={"secret": "PRIVATE"},
        )
    conn.execute("UPDATE requester_profiles SET expires_at='2000-01-01T00:00:00+00:00'")
    before = list(conn.iterdump())
    first = page(conn, query="测试", limit=2)
    second = page(conn, query="测试", limit=2, after_id=first["next_after_id"])
    assert len(first["items"] + second["items"]) == 4
    assert second["next_after_id"] is None
    assert first["items"][0]["expiry"] == "expired"
    assert "PRIVATE" not in str(first)
    assert not page(conn, query="absent")["items"]
    assert list(conn.iterdump()) == before
