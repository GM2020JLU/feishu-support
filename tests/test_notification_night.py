from datetime import UTC, datetime
from uuid import uuid4

import pytest

from k3_support import notification_snooze as snooze
from k3_support.delivery import DeliveryReceipt, deliver_claimed
from k3_support.notification_schedule import window
from k3_support.services import _outbox_tick
from k3_support.store import create_case, enqueue_outbox


def moment(text):
    return datetime.fromisoformat(text)


@pytest.mark.parametrize(
    "when,quiet",
    [
        ("2026-09-07T08:59:59+08:00", True),
        ("2026-09-07T09:00:00+08:00", False),
        ("2026-09-07T17:59:59+08:00", False),
        ("2026-09-07T18:00:00+08:00", True),
    ],
)
def test_daily_boundaries(config, when, quiet):
    assert window(config, moment(when))["active"] is quiet


def test_overnight_schedule_and_dst_use_configured_timezone(config):
    config.raw["work_hours"] = {"start": "22:00", "end": "06:00"}
    assert not window(config, moment("2026-09-07T23:00:00+08:00"))["active"]
    assert not window(config, moment("2026-09-08T05:59:00+08:00"))["active"]
    assert window(config, moment("2026-09-08T06:00:00+08:00"))["active"]
    config.raw["timezone"] = "America/New_York"
    config.raw["work_hours"] = {"start": "09:00", "end": "18:00"}
    assert (
        window(config, moment("2026-03-07T20:00:00-05:00"))["until_at"]
        == "2026-03-08T13:00:00+00:00"
    )
    assert (
        window(config, moment("2026-10-31T20:00:00-04:00"))["until_at"]
        == "2026-11-01T14:00:00+00:00"
    )


def test_night_toggle_preserves_manual_snooze_and_has_stable_replay(conn, config):
    now = moment("2026-09-07T20:00:00+08:00")
    snooze.set_snooze(
        conn,
        minutes=60,
        expected_revision=0,
        request_id=str(uuid4()),
        actor_id="owner",
        now=now,
    )
    original = snooze.status(conn, now=now, config=config)["manual_until_at"]
    args = {
        "minutes": None,
        "night_enabled": True,
        "expected_revision": 1,
        "request_id": str(uuid4()),
        "actor_id": "owner",
        "now": now,
    }
    first = snooze.set_snooze(conn, **args)
    assert snooze.set_snooze(conn, **args)["replayed"]
    state = snooze.status(conn, now=now, config=config)
    assert state["night_enabled"] and state["night_active"] and state["manual_active"]
    assert state["manual_until_at"] == original
    snooze.set_snooze(
        conn,
        minutes=0,
        expected_revision=first["revision"],
        request_id=str(uuid4()),
        actor_id="owner",
        now=now,
    )
    state = snooze.status(conn, now=now, config=config)
    assert state["active"] and not state["manual_active"] and state["night_enabled"]
    snooze.set_snooze(
        conn,
        minutes=None,
        night_enabled=False,
        expected_revision=3,
        request_id=str(uuid4()),
        actor_id="owner",
        now=now,
    )
    assert not snooze.status(conn, now=now, config=config)["active"]


def test_thirty_night_questions_wait_then_one_summary_at_work_start(
    conn, config, monkeypatch
):
    config.raw["mode"] = "active"
    clock = [moment("2026-09-07T20:00:00+08:00")]
    monkeypatch.setattr(
        snooze,
        "_now",
        lambda now=None: (now or clock[0]).astimezone(UTC),
    )
    snooze.set_snooze(
        conn,
        minutes=None,
        night_enabled=True,
        expected_revision=0,
        request_id=str(uuid4()),
        actor_id="owner",
    )
    for index in range(30):
        case, _ = create_case(
            conn, title="synthetic", case_type="bug", severity="P2", confidence=1
        )
        item, _ = enqueue_outbox(
            conn,
            channel="telegram",
            action_type="owner_decision",
            destination="telegram:owner-chat",
            payload={"text": "synthetic"},
            case_id=case,
            idempotency_key=f"night-{index}",
        )
        conn.execute(
            "UPDATE outbox SET created_at=? WHERE outbox_id=?",
            (clock[0].astimezone(UTC).isoformat(), item),
        )
    sent = []

    def delivery(connection, cfg, row, **kwargs):
        def transport(destination, text):
            sent.append(text)
            return DeliveryReceipt("synthetic-night-receipt", {})

        kwargs["telegram_runner"] = transport
        return deliver_claimed(connection, cfg, row, **kwargs)

    assert _outbox_tick(conn, config, delivery=delivery)["delivered"] == 0
    assert sent == []
    clock[0] = moment("2026-09-08T09:00:00+08:00")
    assert _outbox_tick(conn, config, delivery=delivery)["delivered"] == 1
    assert len(sent) == 1 and "30 条提醒" in sent[0]
    assert (
        conn.execute("SELECT count(*) FROM cases WHERE state='resolved'").fetchone()[0]
        == 0
    )
    assert _outbox_tick(conn, config, delivery=delivery)["delivered"] == 0


def test_upgrade_keeps_old_manual_request_idempotent_and_night_disabled(
    config, monkeypatch
):
    from k3_support import db
    from k3_support.ids import digest

    connection = db.connect(config.database_path)
    migrations = db.migration_files()
    identifier = str(uuid4())
    stamp = "2026-09-07T12:00:00+00:00"
    until = "2026-09-07T13:00:00+00:00"
    try:
        with monkeypatch.context() as context:
            context.setattr(
                db,
                "migration_files",
                lambda: [item for item in migrations if item[0] <= 41],
            )
            db.migrate(connection)
        connection.execute(
            "INSERT INTO notification_snooze VALUES(1,1,?,?,?)", (until, "owner", stamp)
        )
        fingerprint = digest({"actor": "owner", "minutes": 60, "expected_revision": 0})
        connection.execute(
            "INSERT INTO notification_snooze_history VALUES(?,?,?,?,?,?)",
            (identifier, fingerprint, 1, until, "owner", stamp),
        )
        assert db.migrate(connection) == [
            item[0] for item in migrations if item[0] > 41
        ]
        result = snooze.set_snooze(
            connection,
            minutes=60,
            expected_revision=0,
            request_id=identifier,
            actor_id="owner",
        )
        assert result["replayed"] and result["until_at"] == until
        assert not result["night_enabled"]
    finally:
        connection.close()
