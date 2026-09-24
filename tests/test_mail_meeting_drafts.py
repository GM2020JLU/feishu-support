from uuid import uuid4

import pytest
from test_mail_snapshot import item

from k3_support import mail_meeting_drafts as drafts


def test_partial_draft_edit_replay_and_no_calendar_effects(conn):
    message, _, _ = item(conn, 1)
    initial = drafts.read(conn, message)
    assert initial["draft"]["attendee_ids"] == []
    assert initial["draft"]["start"] == ""
    request = {
        "message_id": message, "draft": initial["draft"], "expected_revision": 0,
        "source_digest": initial["source_digest"], "request_id": str(uuid4()), "actor_id": "owner",
    }
    saved = drafts.save(conn, **request)
    assert saved["revision"] == 1 and not saved["calendar_created"]
    assert drafts.save(conn, **request)["replayed"]
    with pytest.raises(ValueError, match="changed"):
        drafts.save(conn, **{**request, "request_id": str(uuid4())})
    with pytest.raises(ValueError, match="reused"):
        drafts.save(
            conn, **{**request, "draft": {**request["draft"], "summary": "new"}}
        )
    conn.execute(
        "UPDATE mail_catalog_items SET subject='updated' WHERE message_id=?", (message,)
    )
    assert drafts.read(conn, message)["source_changed"]
    with pytest.raises(ValueError, match="changed"):
        drafts.save(
            conn, **{**request, "expected_revision": 1, "request_id": str(uuid4())}
        )
    for table in ("approvals", "meeting_previews", "outbox"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"attendee_ids": ["person@example.test"]},
        {"timezone": "invalid/zone"},
        {"start": "2099-01-01T09:00:00+00:00"},
        {"description": "@/tmp/private"},
        {"unexpected": True},
    ],
)
def test_invalid_drafts_do_not_persist(conn, change):
    message, _, _ = item(conn, 2)
    current = drafts.read(conn, message)
    with pytest.raises((ValueError, KeyError)):
        drafts.save(
            conn,
            message_id=message,
            draft={**current["draft"], **change},
            expected_revision=0,
            source_digest=current["source_digest"],
            request_id=str(uuid4()),
            actor_id="owner",
        )
    assert conn.execute("SELECT count(*) FROM mail_meeting_drafts").fetchone()[0] == 0
