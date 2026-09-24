from uuid import uuid4

from k3_support.audit_inventory import page
from k3_support.watch_subscriptions import configure


def test_watch_audit_preserves_decisions_and_is_read_only(conn):
    args = {"owner_id": "owner", "source_kind": "release", "source_key": "uboot",
            "enabled": True, "expected_revision": 0, "request_id": str(uuid4()), "repositories": ["uboot"]}
    configure(conn, **args)
    configure(conn, **{**args, "enabled": False, "expected_revision": 1, "request_id": str(uuid4())})
    configure(conn, **args)
    before = list(conn.iterdump())
    first = page(conn, kind="subscriptions", limit=1)
    second = page(conn, kind="subscriptions", cursor=first["next_cursor"], limit=1)
    assert first["total_matching"] == 2
    items = first["items"] + second["items"]
    assert all(item["actor_id"] == "owner" and item["target"] == "release:uboot" for item in items)
    assert any("已订阅" in item["summary"] for item in items)
    assert any("已退订" in item["summary"] for item in items)
    assert len({item["key"] for item in items}) == 2
    assert "request_digest" not in str(items)
    assert list(conn.iterdump()) == before
