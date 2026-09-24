import sqlite3

import pytest
import yaml
from test_broker_grant_schema import grant
from test_broker_task_binding import seed

from k3_support.approvals import request_approval
from k3_support.config import FEATURES, load_config
from k3_support.operations import OperationsError
from k3_support.recovery_bundle import create
from k3_support.recovery_stage import stage
from k3_support.store import enqueue_outbox, ingest_event


def prepare(config, conn, tmp_path):
    config.path.write_text(yaml.safe_dump(config.raw))
    path = config.data_dir / "attachments" / "source"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fixture raw body")
    ingest_event(conn, source="feishu_bot_im", identity="bot", external_id="fixture",
                 payload={}, occurred_at="2026-09-08T00:00:00+00:00", raw_artifact_path=str(path))
    binding = seed(conn)
    grant(conn)
    approval, _, _ = request_approval(conn, approval_type="board1_lease", case_id=binding["case_id"],
                                     action={"test": True}, session_id="fixture",
                                     expires_at="2099-01-01T00:00:00+00:00")
    conn.execute("UPDATE approvals SET status='approved' WHERE approval_id=?", (approval,))
    conn.execute("INSERT INTO locks VALUES('board1','fixture',?,'board','old','future','old','{}')", (binding["case_id"],))
    enqueue_outbox(conn, channel="feishu_im", action_type="reply", destination="fixture-no-send",
                   payload={"text": "pending"}, idempotency_key="fixture", case_id=binding["case_id"])
    bundle = tmp_path.parent / (tmp_path.name + "-bundle")
    target = tmp_path.parent / (tmp_path.name + "-restored")
    create(config, bundle)
    return bundle, target


@pytest.mark.parametrize("mode", [None, "auto_60", "stopped"])
def test_stage_relocates_files_and_revokes_authority_without_touching_source(config, conn, tmp_path, mode):
    if mode:
        conn.execute("INSERT INTO global_control_state VALUES('feishu_support',?,5,8,?,'fixture','fixture','old')",
                     (mode, "2099-01-01T00:00:00+00:00" if mode == "auto_60" else None))
    bundle, target = prepare(config, conn, tmp_path)
    original = conn.serialize()
    locks = [tuple(row) for row in conn.execute("SELECT * FROM locks")]
    result = stage(bundle, target)
    assert result["staged"] and not result["activation_allowed"]
    assert result["fencing"]["approvals_revoked"] == 1
    assert result["fencing"]["grants_revoked"] == 1
    assert not result["fencing"]["processes_stopped"]
    restored = sqlite3.connect(target / "database.db")
    try:
        assert restored.execute("SELECT mode FROM global_control_state").fetchone()[0] == "stopped"
        revision, outbound, expiry = restored.execute("SELECT revision,outbound_fence,auto_expires_at FROM global_control_state").fetchone()
        assert (revision, outbound) == ((6, 9) if mode else (1, 2))
        assert expiry is None
        assert restored.execute("SELECT status FROM approvals").fetchone()[0] == "revoked"
        assert restored.execute("SELECT revoked_at FROM broker_grants").fetchone()[0]
        assert restored.execute("SELECT state FROM jobs").fetchone()[0] == "orphaned"
        assert restored.execute("SELECT state FROM outbox").fetchone()[0] == "cancelled"
        assert list(restored.execute("SELECT * FROM locks")) == locks
        assert restored.execute("SELECT raw_artifact_path FROM inbound_events").fetchone()[0] == str(target / "data/attachments/source")
        assert restored.execute("SELECT count(*) FROM approval_audit_events WHERE after_status='revoked'").fetchone()[0] == 1
        assert not list(restored.execute("PRAGMA foreign_key_check"))
    finally:
        restored.close()
    review = load_config(target / "config.review.yaml")
    assert review.mode == "shadow"
    assert all(not review.feature(name) for name in FEATURES)
    assert (target / "data/attachments/source").read_bytes() == b"fixture raw body"
    assert conn.serialize() == original
    assert not (target / "INCOMPLETE").exists()
    with pytest.raises(FileExistsError):
        stage(bundle, target)


def test_stage_refuses_tampered_bundle_before_creating_destination(config, conn, tmp_path):
    bundle, target = prepare(config, conn, tmp_path)
    (bundle / "data/attachments/source").write_bytes(b"changed")
    with pytest.raises(OperationsError):
        stage(bundle, target)
    assert not target.exists()


def test_stage_interruption_keeps_incomplete_marker_and_no_review_config(config, conn, tmp_path, monkeypatch):
    import k3_support.recovery_stage as recovery

    bundle, target = prepare(config, conn, tmp_path)
    before = conn.serialize()

    def interrupted(_conn):
        raise OperationsError("synthetic interruption")

    monkeypatch.setattr(recovery, "fence", interrupted)
    with pytest.raises(OperationsError, match="interruption"):
        stage(bundle, target)
    assert (target / "INCOMPLETE").exists()
    assert not (target / "config.review.yaml").exists()
    assert not (target / "restore-report.json").exists()
    assert conn.serialize() == before


def test_optional_config_defaults_cannot_reenable_restored_automation(config, conn, tmp_path):
    config.raw.pop("notifications", None)
    config.raw.pop("routing", None)
    bundle, target = prepare(config, conn, tmp_path)
    stage(bundle, target)
    review = load_config(target / "config.review.yaml")
    assert not any(review.raw["notifications"].values())
    assert review.raw["routing"]["ai_enabled"] is False
    assert all(not review.feature(name) for name in FEATURES)


def test_old_approval_and_feature_draft_are_rejected_by_real_handlers(config, conn, tmp_path):
    from k3_support import feature_settings
    from k3_support.approvals import ApprovalError, decide_approval

    enabled = {**config.raw["features"], "codex": True, "board": True}
    first = feature_settings.preview(conn, config, session_id="old-session", values=enabled, expected_revision=0)
    feature_settings.apply(conn, config, session_id="old-session", actor_id="fixture-owner", draft_id=first["draft_id"])
    pending = feature_settings.preview(conn, config, session_id="old-session", values={**enabled, "mail": True}, expected_revision=1)
    bundle, target = prepare(config, conn, tmp_path)
    approval = dict(conn.execute("SELECT * FROM approvals").fetchone())
    stage(bundle, target)
    review = load_config(target / "config.review.yaml")
    assert all(not review.feature(name) for name in FEATURES)
    restored = sqlite3.connect(target / "database.db", isolation_level=None)
    restored.row_factory = sqlite3.Row
    try:
        before = restored.serialize()
        with pytest.raises(ValueError):
            feature_settings.apply(restored, review, session_id="old-session", actor_id="fixture-owner", draft_id=pending["draft_id"])
        with pytest.raises(ApprovalError):
            decide_approval(restored, review, approval_id=approval["approval_id"], approve=True,
                            approver_user_id=review.telegram_control_user_id,
                            approver_chat_id=review.telegram_control_chat_id, message_id="old-button",
                            decision_text="approve", expected_digest=approval["action_digest"])
        assert restored.serialize() == before
    finally:
        restored.close()
