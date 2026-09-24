"""Independent synthetic R1 cross-review, with all external sends injected."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from test_delivery_block_recovery import case_reply, current, refuse
from test_runtime_control import click, issue_and_bind

from k3_support import delivery, delivery_recovery, retrieval
from k3_support.control import ControlMessage, execute_control
from k3_support.coordination import control_communication, ensure_turn
from k3_support.db import transaction
from k3_support.lark import CommandResult
from k3_support.operations import reconcile
from k3_support.store import claim_jobs, ingest_event


def test_queued_block_notice_must_not_interrupt_subsequent_human_claim(conn, config, tmp_path, monkeypatch):
    cfg, _, _, oid, cid, _ = case_reply(conn, config, tmp_path, monkeypatch)
    monkeypatch.setattr(delivery_recovery, "_now", lambda: datetime(2026, 9, 7, 2, 0, tzinfo=UTC))
    refuse(conn, cfg)
    block = current(conn, "delivery_blocks", "outbox_id", oid)
    control_communication(conn, case_id=cid, action="claim", actor_id="owner-user", external_id="owner-after-block")
    assert conn.execute("SELECT count(*) FROM active_delivery_blocks").fetchone()[0] == 0
    calls = []
    row = delivery.claim_outbox(conn, worker_id="notice-sender")
    if row:
        assert row["outbox_id"] == block["notification_outbox_id"]
        try:
            delivery.deliver_claimed(conn, cfg, row, telegram_button_runner=lambda *args, **kwargs: (
                calls.append(args) or delivery.DeliveryReceipt("synthetic-notice-id", {"ok": True})
            ))
        except delivery.DeliverySuppressed:
            pass
    assert not calls, "obsolete block prompt was sent after the owner had already taken over"


def test_new_turn_normally_removes_old_block_from_active_view(conn, config, tmp_path, monkeypatch):
    cfg, _, _, _, cid, tid = case_reply(conn, config, tmp_path, monkeypatch)
    refuse(conn, cfg)
    event_pk, _ = ingest_event(
        conn, source="feishu_user_poll", identity="user", external_id="om_new_turn_after_block",
        payload={"content": "更正：这是新补充", "chat_type": "p2p"},
        occurred_at="2026-09-08T01:00:00+00:00", sender_id="ou_fixture", chat_id="oc_fixture",
    )
    with transaction(conn):
        new_turn = ensure_turn(conn, case_id=cid, source_event_pk=event_pk)
    assert new_turn["turn_id"] != tid
    assert current(conn, "conversation_turns", "turn_id", tid)["state"] == "closed"
    assert conn.execute("SELECT count(*) FROM active_delivery_blocks").fetchone()[0] == 0


def test_post_dispatch_pause_retains_real_receipt_without_reauthorizing_or_notifying(conn, config, tmp_path, monkeypatch):
    cfg, release, policy, oid, _, tid = case_reply(conn, config, tmp_path, monkeypatch)
    calls = []

    def send(argv):
        calls.append(argv)
        panel = issue_and_bind(conn, cfg)
        click(conn, cfg, panel, "global_pause")
        policy["revoked_release_ids"].append(release["release_id"])
        return CommandResult({"message_id": "om_post_pause_receipt"}, "user", [])

    row = delivery.claim_outbox(conn, worker_id="pre-pause-sender")
    receipt = delivery.deliver_claimed(conn, cfg, row, lark_runner=send)
    reconcile(conn, config=cfg)
    assert receipt.remote_id == "om_post_pause_receipt" and len(calls) == 1
    assert current(conn, "outbox", "outbox_id", oid)["state"] == "cancelled"
    events = conn.execute("SELECT * FROM outbox_attempt_events WHERE claim_token=? AND event_type='delivered'", (row["claim_token"],)).fetchall()
    assert len(events) == 1 and events[0]["remote_message_id"] == receipt.remote_id
    assert conn.execute("SELECT count(*) FROM delivery_blocks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox WHERE action_type='owner_decision'").fetchone()[0] == 0
    assert current(conn, "conversation_turns", "turn_id", tid)["state"] == "human_hold"


def test_successful_lookup_does_not_prevent_new_delegated_lookup(conn, config, tmp_path, monkeypatch):
    """Explicit delegation performs fresh work without reviving a refused answer."""
    cfg, _, _, oid, cid, _ = case_reply(conn, config, tmp_path, monkeypatch)
    event_pk = current(conn, "outbox", "outbox_id", oid)["source_event_pk"]
    query = json.loads(current(conn, "inbound_events", "event_pk", event_pk)["payload_json"])["content"]
    old_job, created = retrieval.create_retrieval_job(conn, cfg, case_id=cid, query=query, source_event_pk=event_pk)
    assert created
    conn.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (old_job,))
    refuse(conn, cfg)
    old_next_action = current(conn, "cases", "case_id", cid)["next_action"]
    cfg.raw["features"]["codex"] = True
    result = execute_control(conn, cfg, ControlMessage("owner-user", "owner-chat", "reuse-old-lookup", f"delegate {cid}"))
    fresh_job = result["continuation"]["job_id"]
    assert result["continuation"] == {"job_id": fresh_job, "created": True, "route": "codex_debug"}
    assert fresh_job != old_job
    assert current(conn, "jobs", "job_id", old_job)["state"] == "succeeded"
    assert [row["job_id"] for row in claim_jobs(conn, "fresh-delegated-lookup")] == [fresh_job]
    assert current(conn, "cases", "case_id", cid)["next_action"] != old_next_action
    assert current(conn, "outbox", "outbox_id", oid)["state"] == "cancelled"


def test_fresh_lookup_projection_does_not_overwrite_a_newer_operator_next_action(conn, config, tmp_path, monkeypatch):
    cfg, _, _, oid, cid, _ = case_reply(conn, config, tmp_path, monkeypatch)
    refuse(conn, cfg)
    cfg.raw["features"]["codex"] = True
    create = retrieval.create_retrieval_job

    def intervening_next_action(*args, **kwargs):
        result = create(*args, **kwargs)
        conn.execute("UPDATE cases SET next_action='Synthetic newer operator decision',version=version+1 WHERE case_id=?", (cid,))
        return result

    monkeypatch.setattr(retrieval, "create_retrieval_job", intervening_next_action)
    result = execute_control(conn, cfg, ControlMessage("owner-user", "owner-chat", "preserve-newer-decision", f"delegate {cid}"))
    assert result["continuation"]["created"]
    assert current(conn, "cases", "case_id", cid)["next_action"] == "Synthetic newer operator decision"
    assert current(conn, "outbox", "outbox_id", oid)["state"] == "cancelled"
    assert [row["job_id"] for row in claim_jobs(conn, "fresh-worker")] == [result["continuation"]["job_id"]]
