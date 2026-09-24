from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_mail_actions import arguments
from test_mail_snapshot import item

from k3_support import attention_subscriptions as subscriptions
from k3_support import mail_actions

NOW = datetime(2026, 9, 1, 2, tzinfo=UTC)


def request(**extra):
    return dict(
        owner_id="owner",
        category="build_ci",
        enabled=True,
        expected_revision=0,
        request_id=str(uuid4()),
        now=NOW,
        **extra,
    )


def test_subscription_new_matching_mail_dedupe_disposition_and_unsubscribe(conn):
    args = request()
    sub = subscriptions.configure(conn, **args)
    assert subscriptions.configure(conn, **args) == sub
    first, _, _ = item(conn, 1)
    item(conn, 2, category="upstream")
    item(conn, 3)
    conn.execute(
        "UPDATE mail_catalog_items SET first_seen_at=? WHERE message_id='mail-003'",
        ((NOW - timedelta(days=1)).isoformat(),),
    )
    assert subscriptions.collect(conn)["created"] == 1
    assert subscriptions.collect(conn)["created"] == 0
    before = list(conn.iterdump())
    result = subscriptions.page(conn, owner_id="owner")
    assert [r["message_id"] for r in result["items"]] == [first]
    aid = result["items"][0]["action_id"]
    opened = subscriptions.detail(conn, owner_id="owner", action_id=aid)
    assert opened["message_id"] == first and opened["revision"] == 0
    assert "PRIVATE" not in str(opened)
    with pytest.raises(ValueError, match="不可用"):
        subscriptions.detail(conn, owner_id="other", action_id=aid)
    assert "PRIVATE" not in str(result)
    assert subscriptions.page(conn, owner_id="other")["items"] == []
    assert list(conn.iterdump()) == before
    mail_actions.apply(conn, **arguments(conn, first))
    assert subscriptions.page(conn, owner_id="owner")["items"] == []
    subscriptions.configure(
        conn,
        **{
            **args,
            "request_id": str(uuid4()),
            "expected_revision": 1,
            "enabled": False,
        },
    )
    item(conn, 4)
    assert subscriptions.collect(conn)["created"] == 0
    assert subscriptions.configure(conn, **args) == sub
    assert subscriptions.page(conn, owner_id="owner")["items"] == []
    with pytest.raises(ValueError, match="不可用"):
        subscriptions.detail(conn, owner_id="owner", action_id=aid)


def test_snooze_and_stale_configuration(conn):
    args = request(snooze_minutes=60)
    subscriptions.configure(conn, **args)
    item(conn, 1)
    assert subscriptions.collect(conn, now=NOW)["created"] == 0
    assert subscriptions.collect(conn, now=NOW + timedelta(hours=2))["created"] == 1
    assert subscriptions.page(conn, owner_id="owner", now=NOW)["items"] == []
    assert (
        len(
            subscriptions.page(conn, owner_id="owner", now=NOW + timedelta(hours=2))[
                "items"
            ]
        )
        == 1
    )
    with pytest.raises(ValueError, match="changed"):
        subscriptions.configure(conn, **{**args, "request_id": str(uuid4())})
    with pytest.raises(ValueError, match="binding"):
        subscriptions.configure(conn, **{**args, "enabled": False})
    restored = subscriptions.configure(
        conn,
        **{
            **args,
            "expected_revision": 1,
            "request_id": str(uuid4()),
            "snooze_minutes": None,
        },
    )
    assert restored["snooze_until"] is None
    assert len(subscriptions.page(conn, owner_id="owner", now=NOW)["items"]) == 1
