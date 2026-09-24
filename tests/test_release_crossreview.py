"""Independent regression probes; all signatures and transports are fixtures."""

from __future__ import annotations

import json

import pytest
from test_knowledge_release_delivery import queued_fixture
from test_review import (
    active_config,
    make_job,
    remote_runner,
)
from test_review import (
    test_approved_push_is_single_attempt_verified_and_finally_reviewed as _seed_verified_push,
)
from test_review import (
    test_verified_board_request_creates_one_telegram_gate_without_calling_model as _seed_verified_board,
)

from k3_support import delivery, review
from k3_support.db import connect
from k3_support.executors import ExecutionResult
from k3_support.ids import canonical_json, digest
from k3_support.knowledge_release import verify_knowledge_reply
from k3_support.lark import CommandResult


def _saved_reply(conn, *, outbox_id=None):
    if outbox_id:
        row = conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM outbox WHERE channel='feishu_im' AND action_type='reply'").fetchone()
    assert row is not None
    return dict(row), json.loads(row["payload_json"])


def _deliver(conn, cfg, row):
    calls = []
    claimed = delivery.claim_outbox(
        conn, worker_id="independent-fixture", eligible=lambda item: item["outbox_id"] == row["outbox_id"]
    )
    assert claimed is not None
    receipt = delivery.deliver_claimed(
        conn, cfg, claimed,
        lark_runner=lambda argv: calls.append(argv) or CommandResult({"message_id": "fixture-reviewed-receipt"}, "user", []),
    )
    assert len(calls) == 1
    assert receipt.remote_id == "fixture-reviewed-receipt"
    assert conn.execute("SELECT state FROM outbox WHERE outbox_id=?", (row["outbox_id"],)).fetchone()[0] == "delivered"


def _reply_model(argv, prompt, _timeout):
    data = json.loads(prompt.split("REVIEW INPUT JSON:\n", 1)[1])
    decision_id = prompt.split("hermes-review-", 1)[1].split(".", 1)[0]
    decision = {
        "decision_id": "hermes-review-" + decision_id,
        "case_id": data["case"]["case_id"],
        "expected_case_version": data["case"]["version"],
        "intent": "reply",
        "confidence": 0.95,
        "evidence_ids": data["allowed_evidence_ids"],
        "reply_draft": "[AI 自动回复]已核对本次验证证据；尚未确认对方现场是否恢复。",
        "proposed_actions": data["allowed_reply_action"],
        "facts": ["Case-scoped independent evidence was checked"],
        "inferences": [],
        "unknowns": ["field resolution is not confirmed"],
    }
    return ExecutionResult(argv, 0, canonical_json(decision), "")


def _case_reply(conn, config, kind="git", serial_receipt=None):
    config.raw["coordination"]["work_hours_send_grace_seconds"] = 0
    config.raw["coordination"]["off_hours_send_grace_seconds"] = 0
    if kind == "push":
        # Reuse the existing complete approval -> exact push -> final-review
        # scenario as fixture setup, then independently exercise its Outbox.
        _seed_verified_push(conn, config)
        return active_config(config, wip_push=True)
    cfg = active_config(config, board=kind == "board")
    if kind == "board":
        # Existing scenario performs only injected board/remote operations and
        # leaves a verified continuation bundle after fresh BROM cleanup.
        _seed_verified_board(conn, config, serial_receipt=serial_receipt)
        job_id = conn.execute(
            "SELECT job_id FROM jobs WHERE json_extract(context_json,'$.board_session_id') IS NOT NULL"
        ).fetchone()[0]
    else:
        _, job_id = make_job(conn, cfg)
    result = review.run_hermes_review(
        conn, cfg, job_id=job_id, remote_runner=remote_runner(), hermes_runner=_reply_model
    )
    assert result["applied"]["applied"] is True
    return cfg


def test_relabelled_knowledge_cannot_bypass_missing_release(conn, config, tmp_path, monkeypatch):
    cfg, _, _, _, _, outbox_id = queued_fixture(conn, config, tmp_path, monkeypatch)
    cfg.raw["features"]["codex"] = True
    cfg.raw["knowledge_release"]["artifact_path"] = None
    row, payload = _saved_reply(conn, outbox_id=outbox_id)
    payload.pop("knowledge_release")
    payload["reply_basis"] = "verified_evidence"
    conn.execute("UPDATE outbox SET payload_json=? WHERE outbox_id=?", (canonical_json(payload), outbox_id))
    checked = verify_knowledge_reply(conn, cfg, row=row, payload=payload)
    assert checked == {"ready": False, "reason": "case_reply_has_no_bound_independent_review"}
    with pytest.raises(delivery.DeliverySuppressed, match="bound_independent_review"):
        delivery.deliver_claimed(
            conn, cfg, delivery.claim_outbox(conn, worker_id="independent-fixture"),
            lark_runner=lambda _: pytest.fail("relabeled knowledge reached transport"),
        )


@pytest.mark.parametrize("layer", ["build", "static"])
def test_legacy_model_report_cannot_authorize_queued_verified_reply(conn, config, layer):
    """Even a fully bound old review is not execution proof from a log hash."""
    cfg = _case_reply(conn, config)
    row, payload = _saved_reply(conn)
    assert verify_knowledge_reply(conn, cfg, row=row, payload=payload)["ready"] is True
    review_row = conn.execute("SELECT * FROM codex_reviews WHERE status='decision_applied'").fetchone()
    evidence_id = json.loads(review_row["evidence_ids_json"])[0]
    evidence = conn.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
    old = json.loads(evidence["result"])
    reported = {"kind": "recorded_check", "repo": old["repo"], "name": "claimed-test",
                "layer": layer, "command": ["make", "check"], "exit_code": 0,
                "output_path": "/private/log", "output_sha256": "b" * 64, "verified": True}
    checks = [reported if item == old else item for item in json.loads(review_row["independent_checks_json"])]
    conn.execute("UPDATE evidence SET result=?,artifact_hash=?,evidence_layer=? WHERE evidence_id=?",
                 (canonical_json(reported), "b" * 64, layer, evidence_id))
    conn.execute("UPDATE codex_reviews SET independent_checks_json=? WHERE review_id=?",
                 (canonical_json(checks), review_row["review_id"]))
    assert verify_knowledge_reply(conn, cfg, row=row, payload=payload) == {
        "ready": False, "reason": "case_reply_has_no_bound_independent_review"}
    with pytest.raises(delivery.DeliverySuppressed, match="bound_independent_review"):
        delivery.deliver_claimed(conn, cfg, delivery.claim_outbox(conn, worker_id="legacy-proof"),
                                 lark_runner=lambda _: pytest.fail("unproven test claim reached transport"))


@pytest.mark.parametrize("change", ("query", "thread", "payload_metadata", "identity", "external_id"))
def test_original_event_binding_covers_content_context_and_identity(
    conn, config, tmp_path, monkeypatch, change
):
    cfg, _, _, _, _, outbox_id = queued_fixture(conn, config, tmp_path, monkeypatch)
    row, payload = _saved_reply(conn, outbox_id=outbox_id)
    if change in {"query", "payload_metadata"}:
        value = json.loads(conn.execute("SELECT payload_json FROM inbound_events WHERE event_pk=?", (row["source_event_pk"],)).fetchone()[0])
        value["content" if change == "query" else "chat_type"] = "entirely different context"
        conn.execute("UPDATE inbound_events SET payload_json=? WHERE event_pk=?", (canonical_json(value), row["source_event_pk"]))
    else:
        column = "thread_id" if change == "thread" else change
        replacement = "bot" if change == "identity" else "different"
        conn.execute(f"UPDATE inbound_events SET {column}=? WHERE event_pk=?", (replacement, row["source_event_pk"]))
    checked = verify_knowledge_reply(conn, cfg, row=row, payload=payload)
    assert checked["ready"] is False
    assert checked["reason"] == "context_source_changed"


@pytest.mark.parametrize("change", ("text_with_matching_digest", "destination", "payload_identity"))
def test_signed_answer_does_not_authorize_new_text_or_recipient(
    conn, config, tmp_path, monkeypatch, change
):
    cfg, _, _, _, _, outbox_id = queued_fixture(conn, config, tmp_path, monkeypatch)
    row, payload = _saved_reply(conn, outbox_id=outbox_id)
    if change == "text_with_matching_digest":
        payload["text"] = "[AI 自动回复]这段并不在已审核正文中。"
        payload["knowledge_release"]["text_digest"] = digest(payload["text"])
    elif change == "destination":
        row["destination"] = "different-message-recipient"
    else:
        payload["identity"] = "bot"
    checked = verify_knowledge_reply(conn, cfg, row=row, payload=payload)
    assert checked["ready"] is False, checked


@pytest.mark.parametrize("kind", ("git", "board", "push"))
def test_real_reviewed_case_reply_reaches_delivery_without_a_knowledge_release(conn, config, kind):
    cfg = _case_reply(conn, config, kind)
    row, payload = _saved_reply(conn)
    assert cfg.raw["knowledge_release"]["artifact_path"] is None
    assert payload["reply_basis"] == "verified_evidence"
    checked = verify_knowledge_reply(conn, cfg, row=row, payload=payload)
    assert checked["ready"] is True, checked
    _deliver(conn, cfg, row)


@pytest.mark.parametrize("change", ("text", "evidence_ids", "review_output", "artifact", "access", "job_round"))
def test_case_reply_requires_unchanged_independent_review(conn, config, change):
    cfg = _case_reply(conn, config)
    row, payload = _saved_reply(conn)
    if change == "text":
        payload["text"] += " additional unsupported claim"
    elif change == "evidence_ids":
        payload["evidence_ids"] = []
    elif change == "review_output":
        conn.execute("UPDATE codex_reviews SET hermes_output_json='{}'")
    elif change == "artifact":
        conn.execute("UPDATE evidence SET artifact_hash='wrong'")
    elif change == "access":
        conn.execute("UPDATE case_sources SET requester_access='unknown'")
    else:
        conn.execute("UPDATE jobs SET lifecycle_round=lifecycle_round+1")
    checked = verify_knowledge_reply(conn, cfg, row=row, payload=payload)
    assert checked == {"ready": False, "reason": "case_reply_has_no_bound_independent_review"}


@pytest.mark.parametrize("kind", ("board", "push"))
@pytest.mark.parametrize("change", ("artifact_hash", "revoked_approval", "failed_ledger"))
def test_non_git_evidence_requires_authority_and_verified_ledger(conn, config, kind, change):
    cfg = _case_reply(conn, config, kind)
    row, payload = _saved_reply(conn)
    source_type = "board1_session" if kind == "board" else "gerrit_wip"
    if change == "artifact_hash":
        conn.execute(
            "UPDATE evidence SET artifact_hash='forged' WHERE source_id IN (SELECT source_id FROM case_sources WHERE source_type=?)",
            (source_type,),
        )
    elif change == "revoked_approval":
        changed = conn.execute("UPDATE approvals SET status='revoked' WHERE status='consumed'")
        assert changed.rowcount == 1
    else:
        changed = conn.execute("UPDATE action_ledger SET state='failed' WHERE state='verified'")
        assert changed.rowcount > 0
    checked = verify_knowledge_reply(conn, cfg, row=row, payload=payload)
    assert checked == {"ready": False, "reason": "case_reply_has_no_bound_independent_review"}


@pytest.mark.parametrize("fail_after_decision", (False, True))
def test_review_and_outbox_are_published_atomically(conn, config, monkeypatch, fail_after_decision):
    cfg = active_config(config)
    case_id, job_id = make_job(conn, cfg)
    observer = connect(config.database_path)
    original = review.apply_decision
    checkpoints = []

    def observe(connection, *args, **kwargs):
        applied = original(connection, *args, **kwargs)
        assert connection.in_transaction
        assert observer.execute("SELECT count(*) FROM outbox WHERE case_id=?", (case_id,)).fetchone()[0] == 0
        assert observer.execute("SELECT status FROM codex_reviews WHERE job_id=?", (job_id,)).fetchone()[0] == "verified"
        checkpoints.append(applied)
        if fail_after_decision:
            raise RuntimeError("synthetic crash after decision before review commit")
        return applied

    monkeypatch.setattr(review, "apply_decision", observe)
    try:
        if fail_after_decision:
            with pytest.raises(RuntimeError, match="synthetic crash"):
                review.run_hermes_review(conn, cfg, job_id=job_id, remote_runner=remote_runner(), hermes_runner=_reply_model)
            assert observer.execute("SELECT count(*) FROM outbox WHERE case_id=?", (case_id,)).fetchone()[0] == 0
            assert observer.execute("SELECT status FROM codex_reviews WHERE job_id=?", (job_id,)).fetchone()[0] == "verified"
        else:
            review.run_hermes_review(conn, cfg, job_id=job_id, remote_runner=remote_runner(), hermes_runner=_reply_model)
            assert observer.execute("SELECT count(*) FROM outbox WHERE case_id=?", (case_id,)).fetchone()[0] == 1
            assert observer.execute("SELECT status FROM codex_reviews WHERE job_id=?", (job_id,)).fetchone()[0] == "decision_applied"
        assert len(checkpoints) == 1
    finally:
        observer.close()
