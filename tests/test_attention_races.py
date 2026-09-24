import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

from test_mail_snapshot import item

from k3_support import attention_subscriptions as subscriptions
from k3_support.db import connect


def seed(conn):
    args = {
        "owner_id": "owner",
        "category": "build_ci",
        "enabled": True,
        "expected_revision": 0,
        "request_id": str(uuid4()),
        "now": datetime(2026, 8, 1, tzinfo=UTC),
    }
    subscriptions.configure(conn, **args)
    item(conn, 1)
    return args


def race(config, left, right):
    barrier = threading.Barrier(2, timeout=5)

    def run(action):
        conn = connect(config.database_path)
        try:
            barrier.wait()
            return action(conn)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(run, left), pool.submit(run, right)
        return a.result(timeout=10), b.result(timeout=10)


def test_concurrent_collect_and_unsubscribe_cannot_leave_visible_items(conn, config):
    args = seed(conn)
    collected, disabled = race(
        config,
        subscriptions.collect,
        lambda other: subscriptions.configure(
            other,
            **{
                **args,
                "enabled": False,
                "expected_revision": 1,
                "request_id": str(uuid4()),
            },
        ),
    )
    assert collected["created"] in (0, 1)
    assert disabled["enabled"] == 0 and disabled["revision"] == 2
    assert subscriptions.page(conn, owner_id="owner")["items"] == []
    assert subscriptions.collect(conn)["created"] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_two_collectors_materialize_exactly_one_item(conn, config):
    seed(conn)
    first, second = race(config, subscriptions.collect, subscriptions.collect)
    assert first["created"] + second["created"] == 1
    assert conn.execute("SELECT count(*) FROM attention_actions").fetchone()[0] == 1


def test_concurrent_configuration_does_not_lose_revision(conn, config):
    args = seed(conn)

    def change(other):
        try:
            return subscriptions.configure(
                other,
                **{
                    **args,
                    "enabled": False,
                    "expected_revision": 1,
                    "request_id": str(uuid4()),
                },
            )
        except ValueError as error:
            return str(error)

    results = race(config, change, change)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "subscription changed; reread" in results
    assert (
        conn.execute("SELECT count(*) FROM attention_subscription_history").fetchone()[
            0
        ]
        == 2
    )
