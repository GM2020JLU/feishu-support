from uuid import uuid4

from k3_support.attention_subscriptions import configure
from k3_support.audit_inventory import page


def test_audit_retains_historical_subscription_and_deduplicates_replay(conn):
    args = {
        "owner_id": "operator",
        "category": "upstream",
        "enabled": True,
        "expected_revision": 0,
        "request_id": str(uuid4()),
    }
    configure(conn, **args)
    configure(
        conn,
        **{
            **args,
            "expected_revision": 1,
            "enabled": False,
            "request_id": str(uuid4()),
        },
    )
    configure(conn, **args)
    before = list(conn.iterdump())
    result = page(conn, kind="subscriptions", limit=1)
    assert result["total_matching"] == 2
    first = result["items"][0]
    assert first["actor_id"] == "operator" and first["target"] == "upstream"
    second = page(conn, kind="subscriptions", cursor=result["next_cursor"], limit=1)
    assert {first["key"], second["items"][0]["key"]} == {
        "subscriptions:" + row[0]
        for row in conn.execute("SELECT request_id FROM attention_subscription_history")
    }
    summaries = first["summary"] + second["items"][0]["summary"]
    assert "已订阅" in summaries and "已退订" in summaries
    assert "不代表已发送提醒" in summaries
    assert "result_json" not in str(result) and "request_digest" not in str(result)
    assert list(conn.iterdump()) == before
