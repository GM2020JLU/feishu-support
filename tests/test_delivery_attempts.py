from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta

import pytest
from test_coordination import active_config, make_turn, queue_reply
from test_runtime_control import click, issue_and_bind

from k3_support.config import Config
from k3_support.coordination import control_communication
from k3_support.db import connect, transaction
from k3_support.delivery import (
    DeliveryReceipt,
    DeliverySuppressed,
    claim_outbox,
    deliver_claimed,
)
from k3_support.lark import CommandResult
from k3_support.mail import prepare_summary, upsert_mail_item
from k3_support.operations import reconcile
from k3_support.store import enqueue_outbox


def active(config):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    return Config(raw, config.path)


def enqueue(conn, *, channel="telegram"):
    with transaction(conn):
        return enqueue_outbox(
            conn, channel=channel,
            action_type="notify" if channel == "telegram" else "send",
            destination="telegram:owner-chat" if channel == "telegram" else "oc_test",
            payload={"text": "fixture", "identity": "bot"},
            idempotency_key="attempt-fixture",
        )[0]


def expire(conn, outbox_id):
    conn.execute(
        "UPDATE outbox SET lease_expires_at=? WHERE outbox_id=?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), outbox_id),
    )


def row_for(conn, outbox_id):
    return dict(conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone())


def test_reclaimed_snapshot_same_worker_cannot_send_or_overwrite(conn, config):
    outbox_id = enqueue(conn)
    old = claim_outbox(conn, worker_id="same-worker")
    expire(conn, outbox_id)
    assert reconcile(conn)["reclaimed_outbox"] == 1  # known not-started, safe to reclaim
    current = claim_outbox(conn, worker_id="same-worker")
    assert current["claim_token"] != old["claim_token"]
    assert current["attempt_count"] == old["attempt_count"] + 1
    with pytest.raises(DeliverySuppressed, match="claim lost"):
        deliver_claimed(
            conn, active(config), old,
            telegram_runner=lambda *_: pytest.fail("stale attempt sent"),
        )
    assert row_for(conn, outbox_id) == current


def test_expired_unreconciled_and_legacy_snapshots_are_rejected(conn, config):
    outbox_id = enqueue(conn)
    claimed = claim_outbox(conn, worker_id="worker")
    expire(conn, outbox_id)
    for snapshot in (claimed, {**claimed, "claim_token": None}):
        with pytest.raises(DeliverySuppressed, match="claim lost"):
            deliver_claimed(
                conn, active(config), snapshot,
                telegram_runner=lambda *_: pytest.fail("invalid claim sent"),
            )


def test_mutated_payload_cannot_use_old_claim(conn, config):
    outbox_id = enqueue(conn)
    claimed = claim_outbox(conn, worker_id="worker")
    conn.execute("UPDATE outbox SET payload_json=? WHERE outbox_id=?", ('{"text":"changed"}', outbox_id))
    with pytest.raises(DeliverySuppressed, match="claim lost"):
        deliver_claimed(conn, active(config), claimed)


def test_a_claim_can_dispatch_only_once_even_before_first_call_returns(conn, config):
    outbox_id = enqueue(conn)
    claimed = claim_outbox(conn, worker_id="worker")
    calls = []

    def sender(*_):
        calls.append("outer")
        with pytest.raises(DeliverySuppressed, match="already dispatched"):
            deliver_claimed(
                conn, active(config), claimed,
                telegram_runner=lambda *_: pytest.fail("same attempt dispatched twice"),
            )
        return DeliveryReceipt("sent-1", {"ok": True})

    deliver_claimed(conn, active(config), claimed, telegram_runner=sender)
    assert calls == ["outer"]
    assert row_for(conn, outbox_id)["state"] == "delivered"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE outbox_attempt_events SET remote_message_id='forged'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE outbox_attempts SET dispatch_started_at=NULL")


@pytest.mark.parametrize("failure", [False, True])
def test_late_outcome_preserves_successor_and_keeps_attempt_evidence(conn, config, failure):
    outbox_id = enqueue(conn, channel="feishu_im")
    claimed = claim_outbox(conn, worker_id="old")
    successor = []
    other = connect(config.database_path)

    def sender(_):
        expire(other, outbox_id)
        reconcile(other)
        successor.append(claim_outbox(other, worker_id="new"))
        if failure:
            raise RuntimeError("late transport failure")
        return CommandResult({"message_id": "old-remote"}, "bot", [])

    try:
        if failure:
            with pytest.raises(RuntimeError, match="late transport"):
                deliver_claimed(conn, active(config), claimed, lark_runner=sender)
        else:
            deliver_claimed(conn, active(config), claimed, lark_runner=sender)
        assert row_for(conn, outbox_id) == successor[0]
        event = conn.execute(
            "SELECT * FROM outbox_attempt_events WHERE claim_token=? AND event_type=?",
            (claimed["claim_token"], "failed" if failure else "delivered"),
        ).fetchone()
        assert event is not None
        if not failure:
            assert event["remote_message_id"] == "old-remote"
    finally:
        other.close()


def test_expired_claim_late_receipt_reconciles_without_sending_again(conn, config):
    outbox_id = enqueue(conn)
    claimed = claim_outbox(conn, worker_id="old")

    def sender(*_):
        expire(conn, outbox_id)
        return DeliveryReceipt("late-receipt", {"ok": True})

    deliver_claimed(conn, active(config), claimed, telegram_runner=sender)
    assert row_for(conn, outbox_id)["state"] == "sending"
    assert reconcile(conn)["recovered_delivered"] == 1
    assert row_for(conn, outbox_id)["remote_message_id"] == "late-receipt"
    assert claim_outbox(conn, worker_id="next") is None


@pytest.mark.parametrize("after_dispatch", [False, True])
@pytest.mark.parametrize("late_error", [False, True])
def test_two_connections_linearize_dispatch_against_human_takeover(
    conn, config, monkeypatch, after_dispatch, late_error,
):
    from k3_support import delivery

    cfg = active_config(config)
    case_id, event_pk, turn = make_turn(conn)
    outbox_id = queue_reply(conn, cfg, case_id, event_pk)
    claimed = claim_outbox(conn, worker_id="sender-thread")
    entered = threading.Event()
    proceed = threading.Event()
    real_begin = delivery._begin_dispatch
    errors = []
    calls = []

    def barrier_begin(*args):
        if after_dispatch:
            real_begin(*args)
        entered.set()
        assert proceed.wait(5), "test failed to release dispatch barrier"
        if not after_dispatch:
            real_begin(*args)

    monkeypatch.setattr(delivery, "_begin_dispatch", barrier_begin)

    def runner(argv):
        if "+chat-messages-list" in argv:
            return CommandResult({"messages": [], "has_more": False}, "user", [])
        calls.append(argv)
        if late_error:
            raise RuntimeError("late failure after takeover")
        return CommandResult({"message_id": "inflight-sent"}, "user", [])

    def worker():
        own = connect(config.database_path)
        try:
            deliver_claimed(own, cfg, claimed, lark_runner=runner)
        except Exception as exc:  # noqa: BLE001 - transfer any child-thread failure to assertions in the test thread
            errors.append(exc)
        finally:
            own.close()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5), "sender did not reach dispatch barrier"
        control = control_communication(
            conn, case_id=case_id, action="claim", actor_id="owner-user",
            external_id="takeover-at-barrier",
        )
        assert len(control["in_flight_deliveries"]) == int(after_dispatch)
    finally:
        proceed.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(calls) == int(after_dispatch)
    assert row_for(conn, outbox_id)["state"] == "cancelled"
    live_turn = conn.execute("SELECT * FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)).fetchone()
    assert live_turn["communication_owner"] == "human"
    assert conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()[0] != "resolved"
    if after_dispatch:
        expected = "failed" if late_error else "delivered"
        assert conn.execute("SELECT 1 FROM outbox_attempt_events WHERE claim_token=? AND event_type=?", (claimed["claim_token"], expected)).fetchone()
        assert not errors or late_error
    else:
        assert len(errors) == 1 and isinstance(errors[0], DeliverySuppressed)
        assert conn.execute("SELECT dispatch_started_at FROM outbox_attempts WHERE claim_token=?", (claimed["claim_token"],)).fetchone()[0] is None


def test_process_crash_after_remote_success_does_not_repeat_non_idempotent_send(conn, config, tmp_path):
    outbox_id = enqueue(conn)
    claimed = claim_outbox(conn, worker_id="child")
    marker = tmp_path / "simulated-remote-receipt"
    script = """
import json, os, sys
from pathlib import Path
from k3_support.config import Config
from k3_support.db import connect
from k3_support.delivery import deliver_claimed
raw, snapshot = json.loads(sys.argv[1]), json.loads(sys.argv[2])
conn = connect(raw['paths']['database'])
def transport(*args):
    Path(sys.argv[3]).write_text('remote accepted once', encoding='utf-8')
    os._exit(23)
deliver_claimed(conn, Config(raw, Path('fixture-config')), snapshot, telegram_runner=transport)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(active(config).raw), json.dumps(claimed), str(marker)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 23, result.stderr
    assert marker.read_text() == "remote accepted once"
    assert row_for(conn, outbox_id)["dispatch_started_at"] is not None
    expire(conn, outbox_id)
    assert reconcile(conn)["uncertain_outbox"] == 1
    assert row_for(conn, outbox_id)["state"] == "permanent_failure"
    assert claim_outbox(conn, worker_id="restart") is None


def test_non_idempotent_transport_error_is_not_automatically_retried(conn, config):
    outbox_id = enqueue(conn)
    claimed = claim_outbox(conn, worker_id="sender")

    def timeout(*_):
        raise subprocess.TimeoutExpired("fixture-transport", 30)

    with pytest.raises(subprocess.TimeoutExpired):
        deliver_claimed(conn, active(config), claimed, telegram_runner=timeout)
    assert row_for(conn, outbox_id)["state"] == "permanent_failure"
    assert conn.execute("SELECT event_type FROM outbox_attempt_events").fetchone()[0] == "uncertain"
    assert claim_outbox(conn, worker_id="retry") is None


def test_projection_failure_cannot_roll_back_known_transport_receipt(conn, config):
    outbox_id = enqueue(conn)
    claimed = claim_outbox(conn, worker_id="sender")
    conn.execute("""CREATE TEMP TRIGGER fail_delivery_projection BEFORE UPDATE ON outbox
                    WHEN NEW.state='delivered'
                    BEGIN SELECT RAISE(ABORT,'injected projection failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="projection failure"):
        deliver_claimed(
            conn, active(config), claimed,
            telegram_runner=lambda *_: DeliveryReceipt("known-receipt", {"ok": True}),
        )
    event = conn.execute("SELECT remote_message_id FROM outbox_attempt_events WHERE event_type='delivered'").fetchone()
    assert event[0] == "known-receipt"
    conn.execute("DROP TRIGGER fail_delivery_projection")
    expire(conn, outbox_id)
    assert reconcile(conn)["recovered_delivered"] == 1
    assert row_for(conn, outbox_id)["remote_message_id"] == "known-receipt"
    assert claim_outbox(conn, worker_id="restart") is None


@pytest.mark.parametrize("takeover_before_recovery", [False, True])
def test_recovery_finalizes_turn_once_without_restoring_revoked_authority(
    conn, config, takeover_before_recovery,
):
    cfg = active_config(config)
    case_id, event_pk, turn = make_turn(conn)
    outbox_id = queue_reply(conn, cfg, case_id, event_pk)
    claimed = claim_outbox(conn, worker_id="sender")
    conn.execute("""CREATE TEMP TRIGGER fail_turn_completion BEFORE UPDATE ON conversation_turns
                    WHEN NEW.state='ai_sent'
                    BEGIN SELECT RAISE(ABORT,'injected turn failure'); END""")

    def runner(argv):
        if "+chat-messages-list" in argv:
            return CommandResult({"messages": [], "has_more": False}, "user", [])
        return CommandResult({"message_id": "known-turn-receipt"}, "user", [])

    with pytest.raises(sqlite3.IntegrityError, match="turn failure"):
        deliver_claimed(conn, cfg, claimed, lark_runner=runner)
    assert row_for(conn, outbox_id)["state"] == "sending"
    conn.execute("DROP TRIGGER fail_turn_completion")
    if takeover_before_recovery:
        control_communication(
            conn, case_id=case_id, action="claim", actor_id="owner-user",
            external_id="owner-before-recovery",
        )
    expire(conn, outbox_id)
    result = reconcile(conn, config=cfg)
    assert result["recovered_delivered"] == 1
    assert result["finalized_receipts"] == 1
    actual = conn.execute("SELECT state,communication_owner FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)).fetchone()
    assert tuple(actual) == (("human_hold", "human") if takeover_before_recovery else ("ai_sent", "ai"))
    assert row_for(conn, outbox_id)["effects_finalized_at"] is not None
    event_count = conn.execute("SELECT count(*) FROM case_events").fetchone()[0]
    again = reconcile(conn, config=cfg)
    assert again["recovered_delivered"] == again["finalized_receipts"] == 0
    assert conn.execute("SELECT count(*) FROM case_events").fetchone()[0] == event_count
    assert claim_outbox(conn, worker_id="restart") is None


def test_late_confirmed_receipt_resolves_same_attempt_uncertainty_not_history(conn, config):
    outbox_id = enqueue(conn)
    claimed = claim_outbox(conn, worker_id="sender")

    def sender(*_):
        expire(conn, outbox_id)
        assert reconcile(conn)["uncertain_outbox"] == 1
        return DeliveryReceipt("confirmed-late", {"ok": True})

    deliver_claimed(conn, active(config), claimed, telegram_runner=sender)
    assert row_for(conn, outbox_id)["state"] == "permanent_failure"
    assert reconcile(conn, config=active(config))["recovered_delivered"] == 1
    assert row_for(conn, outbox_id)["state"] == "delivered"
    assert row_for(conn, outbox_id)["remote_message_id"] == "confirmed-late"
    events = [row[0] for row in conn.execute("SELECT event_type FROM outbox_attempt_events ORDER BY recorded_at")]
    assert "lease_expired" in events and "delivered" in events
    assert claim_outbox(conn, worker_id="restart") is None


@pytest.mark.parametrize("share", [False, True])
def test_revoked_mail_recovery_cannot_bypass_finalization_via_legacy_paths(conn, config, share):
    upsert_mail_item(conn, {
        "message_id": "recovery-mail", "subject": "Release blocker",
        "head_from": {"name": "Peer", "mail_address": "peer@example.test"},
        "body_preview": "release blocked", "internal_date": "1788231600000",
    })
    summary = prepare_summary(
        conn, scheduled_at="2026-09-01T12:00:00+08:00",
        destination="telegram:owner-chat", slot="mail_noon",
        share_destination="oc_owner_private" if share else None,
        summarizer=lambda _: {
            "overview": "Release needs attention.",
            "categories": [{"category": "project_release", "count": 1, "summary": "Blocked."}],
            "important": ([{"message_id": "recovery-mail", "summary": "Release blocker",
                           "why_important": "Blocked", "action": "Review", "deadline": "Today"}]
                          if share else []),
        },
    )
    cfg = active(config)
    cfg.raw["features"]["mail"] = True
    row = claim_outbox(conn, worker_id="sender")
    assert row["action_type"] == ("share_to_owner" if share else "mail_summary")
    conn.execute("""CREATE TEMP TRIGGER fail_mail_projection BEFORE UPDATE ON outbox
                    WHEN NEW.state='delivered'
                    BEGIN SELECT RAISE(ABORT,'injected mail projection'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="mail projection"):
        deliver_claimed(
            conn, cfg, row,
            mail_runner=lambda _: CommandResult({"im_message_id": "known-shared-mail"}, "user", []),
            telegram_runner=lambda *_: DeliveryReceipt("known-summary", {"ok": True}),
            telegram_button_runner=lambda *_: DeliveryReceipt("known-summary", {"ok": True}),
        )
    conn.execute("DROP TRIGGER fail_mail_projection")
    expire(conn, row["outbox_id"])
    cfg.raw["features"]["mail"] = False
    recovered = reconcile(conn, config=cfg)
    assert recovered["recovered_delivered"] == recovered["finalized_receipts"] == 1
    assert recovered["recovered_mail_links"] == recovered["recovered_summary_watermarks"] == 0
    assert row_for(conn, row["outbox_id"])["effects_finalized_at"] is not None
    assert conn.execute("SELECT 1 FROM outbox_attempt_events WHERE event_type='suppressed'").fetchone()
    # Turning the feature back on cannot revive effects suppressed at recovery.
    cfg.raw["features"]["mail"] = True
    reconcile(conn, config=cfg)
    assert not conn.execute("SELECT 1 FROM outbox WHERE action_type='resolve_app_link'").fetchone()
    assert not conn.execute("SELECT 1 FROM watermarks WHERE watermark_key='mail_summary_delivered'").fetchone()
    run = conn.execute("SELECT state FROM summary_runs WHERE summary_id=?", (summary["summary_id"],)).fetchone()
    assert run is None or run[0] != "delivered"


@pytest.mark.parametrize("recovery", [False, True])
def test_lazy_auto60_expiry_is_materialized_before_final_fence_check(conn, config, recovery):
    cfg = active_config(config)
    panel = issue_and_bind(conn, cfg)
    click(conn, cfg, panel, "global_auto_60")
    case_id, event_pk, turn = make_turn(conn)
    control_communication(
        conn, case_id=case_id, action="delegate", actor_id="owner-user",
        external_id="explicit-delegate-before-timed-run",
    )
    outbox_id = queue_reply(conn, cfg, case_id, event_pk)
    claimed = claim_outbox(conn, worker_id="sender")
    if recovery:
        conn.execute("""CREATE TEMP TRIGGER fail_expiry_projection BEFORE UPDATE ON outbox
                        WHEN NEW.state='delivered'
                        BEGIN SELECT RAISE(ABORT,'injected expiry projection'); END""")

    def expire_mode():
        conn.execute(
            "UPDATE global_control_state SET auto_expires_at=? WHERE scope='feishu_support'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )

    def sender(argv):
        if "+chat-messages-list" in argv:
            return CommandResult({"messages": [], "has_more": False}, "user", [])
        if not recovery:
            expire_mode()
        return CommandResult({"message_id": "receipt-at-expiry"}, "user", [])

    if recovery:
        with pytest.raises(sqlite3.IntegrityError, match="expiry projection"):
            deliver_claimed(conn, cfg, claimed, lark_runner=sender)
        conn.execute("DROP TRIGGER fail_expiry_projection")
        expire_mode()
        expire(conn, outbox_id)
        reconcile(conn, config=cfg)
    else:
        deliver_claimed(conn, cfg, claimed, lark_runner=sender)
    assert conn.execute("SELECT mode FROM global_control_state").fetchone()[0] == "collaborate"
    assert conn.execute("SELECT 1 FROM outbox_attempt_events WHERE event_type='suppressed'").fetchone()
    assert conn.execute("SELECT state FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)).fetchone()[0] != "ai_sent"
    assert conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()[0] != "resolved"
    assert conn.execute("SELECT 1 FROM outbox_attempt_events WHERE event_type='delivered'").fetchone()
