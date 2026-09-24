from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_mail_snapshot import item, prepare

from k3_support import cli, mail_actions
from k3_support.store import create_case


def arguments(conn, message, action="done", **extra):
    current = mail_actions.view(conn, message)
    return dict(
        message_id=message,
        action=action,
        expected_revision=current["revision"],
        content_digest=current["content_digest"],
        request_id=str(uuid4()),
        actor_id="owner",
        **extra,
    )


def test_disposition_keeps_summary_and_mailbox_immutable(conn):
    message, _, _ = item(conn, 1)
    prepare(conn)
    before = {
        table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
        for table in ("mail_items", "mail_catalog_items", "mail_summary_membership")
    }
    request = arguments(conn, message)
    result = mail_actions.apply(conn, **request)
    assert result["effective_state"] == "done" and not result["mailbox_changed"]
    assert "body_preview" not in str(result)
    mail_actions.apply(conn, **arguments(conn, message, "reopen"))
    replay = mail_actions.apply(conn, **request)
    assert replay["replayed"] and replay["revision"] == 1
    assert mail_actions.view(conn, message)["state"] == "todo"
    for table, rows in before.items():
        assert [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")] == rows
    with pytest.raises(ValueError, match="reused"):
        mail_actions.apply(conn, **{**request, "action": "reopen"})


def test_snooze_expiry_links_and_stale_view(conn):
    message, _, _ = item(conn, 2)
    now = datetime(2026, 9, 7, tzinfo=UTC)
    request = arguments(conn, message, "snooze", minutes=60)
    mail_actions.apply(conn, now=now, **request)
    assert mail_actions.view(conn, message, now=now)["effective_state"] == "snoozed"
    assert (
        mail_actions.view(conn, message, now=now + timedelta(hours=1))[
            "effective_state"
        ]
        == "todo"
    )
    assert (
        mail_actions.apply(conn, now=now + timedelta(minutes=20), **request)[
            "snooze_until"
        ]
        == (now + timedelta(hours=1)).isoformat()
    )
    case, _ = create_case(
        conn, title="test", case_type="bug", severity="P2", confidence=1
    )
    before = tuple(
        conn.execute("SELECT * FROM cases WHERE case_id=?", (case,)).fetchone()
    )
    mail_actions.apply(conn, **arguments(conn, message, "link_case", case_id=case))
    assert mail_actions.view(conn, message)["linked_case_id"] == case
    mail_actions.apply(conn, **arguments(conn, message, "unlink_case"))
    assert mail_actions.view(conn, message)["linked_case_id"] is None
    assert (
        tuple(conn.execute("SELECT * FROM cases WHERE case_id=?", (case,)).fetchone())
        == before
    )
    stale = arguments(conn, message)
    conn.execute(
        "UPDATE mail_catalog_items SET subject='changed' WHERE message_id=?", (message,)
    )
    with pytest.raises(ValueError, match="changed"):
        mail_actions.apply(conn, **stale)


def test_invalid_actions_never_write(conn):
    message, _, _ = item(conn, 3)
    for override in (
        {"minutes": 1},
        {"actor_id": ""},
        {"expected_revision": True},
        {"action": "snooze", "minutes": True},
        {"action": "link_case", "case_id": "missing"},
    ):
        with pytest.raises(ValueError):
            mail_actions.apply(conn, **{**arguments(conn, message), **override})
    assert conn.execute("SELECT count(*) FROM mail_action_history").fetchone()[0] == 0


def test_cli_authorizes_before_opening_write_database(monkeypatch):
    from argparse import Namespace

    monkeypatch.setattr(cli, "_config", lambda args: object())

    def reject(*args):
        raise ValueError("unauthorized")

    monkeypatch.setattr(cli, "_control", reject)
    monkeypatch.setattr(
        cli, "_conn", lambda args: pytest.fail("write before authorization")
    )
    with pytest.raises(ValueError, match="unauthorized"):
        cli.cmd_mail_action(Namespace())


def test_two_connections_only_one_stale_action_wins(conn):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from k3_support.db import connect

    message, _, _ = item(conn, 4)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    requests = [arguments(conn, message, action) for action in ("done", "reopen")]
    barrier = Barrier(2)

    def submit(request):
        local = connect(path)
        try:
            barrier.wait(timeout=5)
            try:
                return mail_actions.apply(local, **request)["revision"]
            except ValueError as exc:
                assert "changed" in str(exc)
                return "stale"
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, requests))
    assert set(outcomes) == {1, "stale"}
    assert conn.execute("SELECT count(*) FROM mail_action_history").fetchone()[0] == 1


def test_mail_page_union_keyset_and_bounds(conn):
    for number in range(3):
        item(conn, number)
    first = mail_actions.page(conn, limit=2)
    second = mail_actions.page(conn, after_id=first["next_cursor"], limit=2)
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert second["next_cursor"] is None and first["live"]
    assert len({v["message_id"] for v in first["items"] + second["items"]}) == 3
    with pytest.raises(ValueError):
        mail_actions.page(conn, limit=500)


def test_filters_before_pagination_expiry_and_reclassification(conn):
    for number in range(35):
        item(conn, number)
    message, _, _ = item(conn, 40, category="upstream")
    now = datetime(2026, 9, 7, tzinfo=UTC)
    mail_actions.apply(conn, **arguments(conn, message, "snooze", minutes=60), now=now)
    assert not mail_actions.page(conn, category="upstream", state="todo", now=now)["items"]
    found = mail_actions.page(conn, category="upstream", state="snoozed", now=now)
    assert [v["message_id"] for v in found["items"]] == [message]
    expired = mail_actions.page(conn, category="upstream", state="todo", now=now+timedelta(hours=1))
    assert expired["items"][0]["effective_state"] == "todo"
    stale = arguments(conn, message)
    conn.execute("UPDATE mail_catalog_items SET category='company' WHERE message_id=?", (message,))
    with pytest.raises(ValueError, match="changed"):
        mail_actions.apply(conn, **stale)
    conn.execute("DELETE FROM mail_catalog_items WHERE message_id=?", (message,))
    assert mail_actions.page(conn, category="unclassified")["items"][0]["message_id"] == message
    for kwargs in ({"category":"invented"},{"state":"invented"}):
        with pytest.raises(ValueError):
            mail_actions.page(conn, **kwargs)
