import copy
from datetime import datetime

import pytest

from k3_support.config import Config, ConfigError, is_work_time
from k3_support.delivery_recovery import _notification_time
from k3_support.notification_schedule import window
from k3_support.work_hours_settings import apply, preview, snapshot


def draft(conn, config, values=None):
    return preview(
        conn,
        config,
        values=values or {"start": "10:00", "end": "19:00"},
        expected_revision=snapshot(conn, config)["revision"],
        session_id="session",
    )


def test_effective_hours_change_all_daily_scheduling_without_mutating_base(
    conn, config
):
    original = copy.deepcopy(config.raw)
    at = datetime.fromisoformat("2026-09-07T09:30:00+08:00")
    assert is_work_time(config, at)
    pending = draft(conn, config)
    assert config.work_hours == original["work_hours"]
    result = apply(
        conn,
        config,
        draft_id=pending["draft_id"],
        session_id="session",
        actor_id="owner",
    )
    assert result["revision"] == 1
    assert config.work_hours == pending["proposed"] and config.raw == original
    assert not is_work_time(config, at)
    assert _notification_time(config, at) == window(config, at)["until_at"]
    assert apply(
        conn,
        config,
        draft_id=pending["draft_id"],
        session_id="session",
        actor_id="owner",
    )["replayed"]


def test_stale_draft_and_foreign_session_rejected(conn, config):
    first, second = draft(conn, config), draft(conn, config)
    with pytest.raises(ValueError):
        apply(
            conn,
            config,
            draft_id=first["draft_id"],
            session_id="other",
            actor_id="owner",
        )

    apply(
        conn, config, draft_id=first["draft_id"], session_id="session", actor_id="owner"
    )
    with pytest.raises(ValueError):
        apply(
            conn,
            config,
            draft_id=second["draft_id"],
            session_id="session",
            actor_id="owner",
        )


def test_rollback_is_only_a_draft_until_applied_and_adds_revision(conn, config):
    from k3_support.work_hours_settings import history

    original = config.work_hours
    changed = draft(conn, config)
    apply(
        conn,
        config,
        draft_id=changed["draft_id"],
        session_id="session",
        actor_id="owner",
    )
    rollback = preview(
        conn, config, expected_revision=1, session_id="session", rollback_revision=1
    )
    assert rollback["proposed"] == original
    assert config.work_hours == changed["proposed"]
    assert (
        apply(
            conn,
            config,
            draft_id=rollback["draft_id"],
            session_id="session",
            actor_id="owner",
        )["revision"]
        == 2
    )
    assert config.work_hours == original
    assert len(history(conn)) == 2
    with pytest.raises(ValueError):
        preview(
            conn,
            config,
            values=original,
            expected_revision=2,
            session_id="session",
            rollback_revision=1,
        )
    with pytest.raises(ValueError):
        preview(
            conn,
            config,
            expected_revision=2,
            session_id="session",
            rollback_revision=999,
        )


def test_expired_draft_and_changed_base_fail_closed(conn, config):
    pending = draft(conn, config)
    conn.execute("UPDATE work_hours_drafts SET expires_at='2000-01-01T00:00:00+00:00'")
    with pytest.raises(ValueError):
        apply(
            conn,
            config,
            draft_id=pending["draft_id"],
            session_id="session",
            actor_id="owner",
        )
    fresh = draft(conn, config)
    apply(
        conn, config, draft_id=fresh["draft_id"], session_id="session", actor_id="owner"
    )
    raw = copy.deepcopy(config.raw)
    raw["timezone"] = "UTC"
    with pytest.raises(ConfigError):
        _ = Config(raw, config.path).work_hours


@pytest.mark.parametrize(
    "values",
    [
        {"start": "10:00", "end": "10:00"},
        {"start": "25:00", "end": "09:00"},
        {"start": True, "end": "09:00"},
        {},
    ],
)
def test_invalid_hours(conn, config, values):
    with pytest.raises(ValueError):
        preview(conn, config, values=values, expected_revision=0, session_id="session")


def test_base_migration_explicit_bound_and_audited(conn, config):
    pending = draft(conn, config)
    apply(
        conn,
        config,
        draft_id=pending["draft_id"],
        session_id="session",
        actor_id="owner",
    )
    raw = copy.deepcopy(config.raw)
    raw["timezone"] = "UTC"
    changed = Config(raw, config.path)
    with pytest.raises(ValueError):
        draft(conn, changed)
    state = snapshot(conn, changed, allow_mismatch=True)
    assert state["needs_migration"]
    migration = preview(
        conn,
        changed,
        values=state["values"],
        expected_revision=1,
        session_id="session",
        migrate=True,
    )
    with pytest.raises(ConfigError):
        _ = changed.work_hours
    newer = copy.deepcopy(raw)
    newer["work_hours"] = {"start": "08:00", "end": "17:00"}
    with pytest.raises(ValueError):
        apply(
            conn,
            Config(newer, config.path),
            draft_id=migration["draft_id"],
            session_id="session",
            actor_id="owner",
        )
    result = apply(
        conn,
        changed,
        draft_id=migration["draft_id"],
        session_id="session",
        actor_id="owner",
    )
    assert result["revision"] == 2
    assert changed.work_hours == state["values"]
    with pytest.raises(ConfigError):
        _ = config.work_hours
    audit = conn.execute("SELECT * FROM work_hours_rebases").fetchone()
    assert audit["source_base_digest"] == state["stored_base_digest"]
    assert audit["target_base_digest"] == state["base_digest"]
    assert audit["target_timezone"] == "UTC"
    assert apply(
        conn,
        changed,
        draft_id=migration["draft_id"],
        session_id="session",
        actor_id="owner",
    )["replayed"]
