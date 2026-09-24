from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_attention_races import race

from k3_support.store import create_case
from k3_support.watch_subscriptions import (
    collect_cases,
    collect_releases,
    configure,
    page,
)


def seed(conn, kind):
    if kind == "case":
        key, _ = create_case(conn, title="synthetic", case_type="bug", severity="P3", confidence=0.8)
        collect = collect_cases
    else:
        key = "uboot"
        conn.execute("INSERT INTO release_impacts VALUES('impact','uboot','change','abcdef1','title',NULL,'[]','digest','{}',NULL,'2026-09-01')")
        collect = collect_releases
    args = {"owner_id": "owner", "source_kind": kind, "source_key": key, "enabled": True,
            "expected_revision": 0, "request_id": str(uuid4()), "repositories": ["uboot"],
            "now": datetime(2020, 1, 1, tzinfo=UTC)}
    configure(conn, **args)
    return args, lambda other: collect(other, owner_id="owner")


@pytest.mark.parametrize("kind", ["case", "release"])
def test_two_watch_collectors_deduplicate(conn, config, kind):
    _, collect = seed(conn, kind)
    left, right = race(config, collect, collect)
    assert left["created"] + right["created"] == 1
    assert conn.execute("SELECT count(*) FROM watch_actions").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["case", "release"])
def test_watch_unsubscribe_hides_concurrent_collection(conn, config, kind):
    args, collect = seed(conn, kind)
    collected, disabled = race(config, collect, lambda other: configure(
        other, **{**args, "enabled": False, "expected_revision": 1, "request_id": str(uuid4())}))
    assert collected["created"] in (0, 1)
    assert disabled["revision"] == 2
    assert page(conn, owner_id="owner")["items"] == []
    assert collect(conn)["created"] == 0


def test_watch_concurrent_settings_preserve_revision(conn, config):
    args, _ = seed(conn, "case")

    def change(other):
        try:
            return configure(other, **{**args, "enabled": False, "expected_revision": 1, "request_id": str(uuid4())})
        except ValueError as error:
            return str(error)

    results = race(config, change, change)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "subscription changed; reread" in results
    assert conn.execute("SELECT count(*) FROM watch_subscription_history").fetchone()[0] == 2
