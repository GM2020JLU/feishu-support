from datetime import UTC, datetime
from uuid import uuid4

import pytest

from k3_support.store import create_case
from k3_support.watch_subscriptions import (
    collect_cases,
    collect_releases,
    configure,
    page,
)


def test_watch_case_collects_events_once_without_changing_case(conn):
    case_id, _ = create_case(conn, title="synthetic", case_type="bug", severity="P3", confidence=0.8)
    conn.execute("UPDATE case_events SET created_at='2026-08-01' WHERE case_id=?", (case_id,))
    configure(conn, owner_id="owner", source_kind="case", source_key=case_id, enabled=True,
              expected_revision=0, request_id=str(uuid4()), now=datetime(2026, 9, 1, tzinfo=UTC))
    for number in (2, 3):
        conn.execute("""INSERT INTO case_events(event_id,case_id,sequence,event_type,actor_type,
                        before_state,after_state,detail_json,created_at,created_epoch)
                        VALUES(?,?,?,'state_changed','system','old','new','{}','2026-09-02',0)""",
                     (f"event-{number}", case_id, number))
    before = dict(conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone())
    now = datetime(2026, 9, 8, tzinfo=UTC)
    assert collect_cases(conn, owner_id="other", now=now)["created"] == 0
    assert collect_cases(conn, owner_id="owner", limit=1, now=now)["created"] == 1
    assert collect_cases(conn, owner_id="owner", limit=1, now=now)["created"] == 1
    assert collect_cases(conn, owner_id="owner", now=now)["created"] == 0
    snapshot = list(conn.iterdump())
    first = page(conn, owner_id="owner", limit=1)
    second = page(conn, owner_id="owner", after_id=first["next_after_id"], limit=1)
    assert {first["items"][0]["source_id"], second["items"][0]["source_id"]} == {"event-2", "event-3"}
    assert second["next_after_id"] is None
    assert page(conn, owner_id="other")["items"] == []
    assert "detail_json" not in str(first)
    assert list(conn.iterdump()) == snapshot
    assert dict(conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()) == before
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_watch_release_revision_replay_and_collection(conn):
    request = {"owner_id": "owner", "source_kind": "release", "source_key": "uboot", "enabled": True,
               "expected_revision": 0, "request_id": str(uuid4()), "repositories": ["uboot"],
               "now": datetime(2026, 9, 1, tzinfo=UTC)}
    first = configure(conn, **request)
    assert configure(conn, **request) == first
    with pytest.raises(ValueError, match="binding"):
        configure(conn, **{**request, "enabled": False})
    with pytest.raises(ValueError, match="changed"):
        configure(conn, **{**request, "request_id": str(uuid4())})
    for index, when in enumerate(["2026-08-01", "2026-09-02", "2027-01-01"]):
        conn.execute("INSERT INTO release_impacts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (str(index), "uboot", "change", str(index), "title", None, "[]", str(index), "{}", None, when))
    now = datetime(2026, 9, 8, tzinfo=UTC)
    assert collect_releases(conn, owner_id="other", now=now)["created"] == 0
    assert collect_releases(conn, owner_id="owner", now=now) == {"created": 1, "external_messages_sent": 0}
    assert collect_releases(conn, owner_id="owner", now=now)["created"] == 0
    assert page(conn, owner_id="owner")["items"][0]["revision"] == "1"
    assert conn.execute("SELECT source_id FROM watch_actions").fetchone()[0] == "1"
    disabled = configure(conn, **{**request, "enabled": False, "expected_revision": 1,
                                 "repositories": [], "request_id": str(uuid4())})
    assert disabled["revision"] == 2
    assert configure(conn, **request) == first  # Replay never re-enables.
    assert conn.execute("SELECT enabled FROM watch_subscriptions").fetchone()[0] == 0
    assert page(conn, owner_id="owner")["items"] == []
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("kind,key", [("release", "missing"), ("case", "missing")])
def test_watch_unknown_target_cannot_be_enabled(conn, kind, key):
    before = conn.total_changes
    with pytest.raises(ValueError):
        configure(conn, owner_id="owner", source_kind=kind, source_key=key, enabled=True,
                  expected_revision=0, request_id=str(uuid4()))
    assert conn.total_changes == before
