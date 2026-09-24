import json
import sqlite3

import pytest

from k3_support.mail import prepare_summary
from k3_support.mail_digest_inventory import detail, page


def prepare(conn, channel, destination):
    return prepare_summary(conn, scheduled_at="2026-09-16T12:00:00+08:00",
                           slot="mail_noon", notification_channel=channel, destination=destination)


def test_web_summary_is_visible_without_delivery_or_watermark(conn):
    result = prepare(conn, "web", "web")
    assert result["state"] == "prepared" and result["telegram_outbox_id"] is None
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM watermarks WHERE watermark_key='mail_summary_delivered'").fetchone()[0] == 0
    assert page(conn)["items"][0]["digest_id"] == result["digest_id"]
    before = conn.total_changes
    assert detail(conn, digest_id=result["digest_id"])["read_only"]
    assert conn.total_changes == before


def test_feishu_summary_freezes_route_and_only_sends_notice(conn):
    result = prepare(conn, "feishu_im", "configured-chat")
    row = conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (result["telegram_outbox_id"],)).fetchone()
    assert row["channel"] == "feishu_im" and row["destination"] == "configured-chat"
    payload = json.loads(row["payload_json"])
    assert "网页控制台" in payload["text"] and "buttons" not in payload
    repeated = prepare(conn, "telegram", "telegram:changed-chat")
    assert repeated["notification_channel"] == "feishu_im"
    assert repeated["telegram_outbox_id"] == result["telegram_outbox_id"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE mail_digest_runs SET telegram_destination='other' WHERE digest_id=?", (result["digest_id"],))
