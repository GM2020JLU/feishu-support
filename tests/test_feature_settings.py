import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from k3_support import feature_settings as settings
from k3_support.config import Config
from k3_support.db import connect
from k3_support.runtime_control import capability_allowed


def draft(conn, config, *, session="gui-one", revision=0, **changes):
    values = {**settings.snapshot(conn, config)["values"], **changes}
    return settings.preview(
        conn, config, session_id=session, values=values, expected_revision=revision
    )


def apply(conn, config, value, session="gui-one"):
    return settings.apply(
        conn, config, session_id=session, actor_id="owner", draft_id=value["draft_id"]
    )


def test_draft_is_inert_apply_is_live_and_rollback_is_new_revision(conn, config):
    worker_config = Config(copy.deepcopy(config.raw), config.path)
    value = draft(conn, config, codex=True)
    assert not worker_config.feature("codex")
    assert apply(conn, config, value)["revision"] == 1
    assert worker_config.feature("codex")
    # Enabling a feature never lifts the installation's Shadow gate.
    assert not capability_allowed(conn, config, "codex")
    assert apply(conn, config, value)["replayed"]
    rollback = settings.preview(
        conn,
        config,
        session_id="gui-one",
        values=None,
        expected_revision=1,
        rollback_revision=1,
    )
    assert worker_config.feature("codex")
    assert apply(conn, config, rollback)["revision"] == 2
    assert not worker_config.feature("codex")
    assert (
        conn.execute("SELECT count(*) FROM feature_settings_history").fetchone()[0] == 2
    )
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_stale_cross_session_expired_and_base_changes_are_rejected(conn, config):
    one = draft(conn, config, codex=True)
    two = draft(conn, config, mail=True)
    with pytest.raises(ValueError, match="当前会话"):
        apply(conn, config, one, session="other-browser")
    apply(conn, config, one)
    with pytest.raises(ValueError, match="旧草稿"):
        apply(conn, config, two)
    expired = draft(conn, config, revision=1, mail=True)
    conn.execute(
        "UPDATE feature_settings_drafts SET expires_at='2000-01-01T00:00:00+00:00' WHERE draft_id=?",
        (expired["draft_id"],),
    )
    with pytest.raises(ValueError, match="过期"):
        apply(conn, config, expired)
    config.raw["work_hours"]["start"] = "10:00"
    assert not config.feature("codex")
    with pytest.raises(ValueError, match="基础配置"):
        settings.snapshot(conn, config)
    assert settings.view(conn, config)["requires_rebase"]


@pytest.mark.parametrize(
    "value", [{"mail": True}, {"unknown": True}, {"mail": "false"}]
)
def test_invalid_full_feature_values_cannot_create_drafts(conn, config, value):
    with pytest.raises(ValueError):
        settings.preview(
            conn, config, session_id="gui-one", values=value, expected_revision=0
        )
    assert (
        conn.execute("SELECT count(*) FROM feature_settings_drafts").fetchone()[0] == 0
    )


def test_concurrent_apply_has_one_winner(conn, config):
    one = draft(conn, config, codex=True)
    two = draft(conn, config, mail=True)
    barrier = threading.Barrier(2)

    def contender(value):
        local = connect(config.database_path)
        try:
            barrier.wait(timeout=5)
            try:
                return apply(local, config, value)["applied"]
            except ValueError:
                return False
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(contender, (one, two))) == [False, True]
    assert (
        conn.execute("SELECT count(*) FROM feature_settings_history").fetchone()[0] == 1
    )


def test_corrupt_runtime_values_fail_closed_without_reinitializing(conn, config):
    apply(conn, config, draft(conn, config, codex=True))
    conn.execute("UPDATE feature_settings SET values_json='{}'")
    before = conn.serialize()
    assert not any(settings.effective(config).values())
    assert conn.serialize() == before


def test_live_switch_is_enforced_at_existing_delivery_gate(conn, config):
    from k3_support.delivery import _feature_allowed

    config.raw["mode"] = "active"
    row = {"channel": "feishu_im", "action_type": "reply", "payload_json": "{}"}
    # The existing sender performs this gate again before external transport.
    assert not _feature_allowed(conn, config, row, {})
    apply(conn, config, draft(conn, config, auto_faq=True))
    assert _feature_allowed(conn, config, row, {})
    apply(conn, config, draft(conn, config, revision=1, auto_faq=False))
    assert not _feature_allowed(conn, config, row, {})


def test_readiness_uses_effective_features_without_writing(conn, config):
    from k3_support.readiness import readiness_report

    apply(conn, config, draft(conn, config, codex=True))
    before = conn.serialize()
    report = readiness_report(conn, config)
    assert "codex" in report["authority_boundary"]["enabled_capabilities"]
    assert conn.serialize() == before


def test_unavailable_settings_are_explicit_blocker_not_intentional_off(conn, config):
    from k3_support.readiness import readiness_report

    apply(conn, config, draft(conn, config, codex=True))
    conn.execute("UPDATE feature_settings SET values_json='{}'")
    before = conn.serialize()
    observed = settings.observation(config)
    assert observed["state"] == "unavailable"
    assert observed["revision"] is None
    assert not any(observed["values"].values())
    report = readiness_report(conn, config)
    assert report["authority_boundary"]["feature_settings"]["state"] == "unavailable"
    assert any(
        item["code"] == "feature_settings_unavailable" for item in report["blockers"]
    )
    assert conn.serialize() == before


def test_intentional_all_off_override_remains_verifiable(conn, config):
    values = dict.fromkeys(config.raw["features"], False)
    value = settings.preview(
        conn, config, session_id="gui-one", values=values, expected_revision=0
    )
    apply(conn, config, value)
    observed = settings.observation(config)
    assert observed == {"state": "override", "revision": 1, "values": values}


def test_explicit_rebase_does_not_inherit_old_switches_or_enable_old_worker(
    conn, config
):
    apply(conn, config, draft(conn, config, codex=True))
    old_worker = Config(copy.deepcopy(config.raw), config.path)
    config.raw["work_hours"]["start"] = "10:00"
    before = conn.serialize()
    editor = settings.view(conn, config)
    assert conn.serialize() == before
    assert editor["requires_rebase"] and editor["historical_values"]["codex"]
    assert not any(editor["values"].values())
    with pytest.raises(ValueError, match="明确预览迁移"):
        settings.preview(
            conn,
            config,
            session_id="gui-one",
            values=editor["values"],
            expected_revision=1,
        )
    proposed = settings.preview(
        conn,
        config,
        session_id="gui-one",
        values={**editor["values"], "mail": True},
        expected_revision=1,
        rebase=True,
    )
    assert proposed["base_change"]["from"] != proposed["base_change"]["to"]
    assert not config.feature("mail")
    assert apply(conn, config, proposed)["revision"] == 2
    assert config.feature("mail") and not config.feature("codex")
    assert not old_worker.feature("codex") and not old_worker.feature("mail")
    assert not settings.view(conn, config)["requires_rebase"]
    # Restoring the migrated revision's previous state means all-off on the new
    # base, not resurrecting the old base or its previously enabled capabilities.
    rollback = settings.preview(
        conn,
        config,
        session_id="gui-one",
        values=None,
        expected_revision=2,
        rollback_revision=2,
    )
    apply(conn, config, rollback)
    assert not any(settings.effective(config).values())


def test_rebase_can_confirm_all_off_and_rejects_another_base_change(conn, config):
    apply(conn, config, draft(conn, config, codex=True))
    config.raw["work_hours"]["start"] = "10:00"
    values = settings.view(conn, config)["values"]
    proposed = settings.preview(
        conn,
        config,
        session_id="gui-one",
        values=values,
        expected_revision=1,
        rebase=True,
    )
    assert proposed["changes"] == [] and proposed["rebase"]
    config.raw["work_hours"]["start"] = "11:00"
    with pytest.raises(ValueError, match="旧草稿"):
        apply(conn, config, proposed)
    fresh = settings.preview(
        conn,
        config,
        session_id="gui-one",
        values=values,
        expected_revision=1,
        rebase=True,
    )
    apply(conn, config, fresh)
    assert settings.observation(config)["state"] == "override"
    assert not any(config.feature(key) for key in values)


def test_rebase_rejects_corruption_and_old_draft_cannot_cross_base(conn, config):
    apply(conn, config, draft(conn, config, codex=True))
    pending = draft(conn, config, revision=1, mail=True)
    # A migrated 038 draft has no source_base_digest and must retain its old fence.
    conn.execute(
        "UPDATE feature_settings_drafts SET source_base_digest=NULL WHERE draft_id=?",
        (pending["draft_id"],),
    )
    config.raw["work_hours"]["start"] = "10:00"
    with pytest.raises(ValueError, match="旧草稿"):
        apply(conn, config, pending)
    conn.execute("UPDATE feature_settings SET values_json='{}'")
    with pytest.raises(ValueError, match="布尔开关"):
        settings.view(conn, config)


def test_real_038_upgrade_preserves_pending_draft_fence(config, monkeypatch):
    from k3_support import db
    from k3_support.ids import canonical_json, digest

    connection = db.connect(config.database_path)
    migrations = db.migration_files()
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                db,
                "migration_files",
                lambda: [item for item in migrations if item[0] <= 38],
            )
            db.migrate(connection)
        before = canonical_json(config.raw["features"])
        selected = canonical_json({**config.raw["features"], "mail": True})
        connection.execute(
            "INSERT INTO feature_settings_drafts VALUES(?,?,?,?,?,?,?,?,NULL)",
            (
                "legacy-draft",
                "gui-one",
                0,
                digest(config.raw),
                before,
                selected,
                "2100-01-01T00:00:00+00:00",
                "2026-09-07T00:00:00+00:00",
            ),
        )
        assert db.migrate(connection) == [item[0] for item in migrations if item[0] > 38]
        upgraded = connection.execute(
            "SELECT * FROM feature_settings_drafts WHERE draft_id='legacy-draft'"
        ).fetchone()
        assert upgraded["source_base_digest"] is None
        assert (
            upgraded["values_json"] == selected and upgraded["previous_json"] == before
        )
        assert apply(connection, config, {"draft_id": "legacy-draft"})["revision"] == 1
        assert config.feature("mail")
    finally:
        connection.close()
