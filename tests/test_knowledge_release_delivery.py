from __future__ import annotations

import json

import pytest
from test_knowledge_release import signed_fixture, observed_fixture_selector

from k3_support import delivery
from k3_support.conversation_context import admit_im_event, resolve_event_context
from k3_support.coordination import bind_ai_communication, ensure_turn
from k3_support.db import transaction
from k3_support.ids import canonical_json, digest
from k3_support.knowledge_answer import approved_answer_markdown
from k3_support.knowledge_release import bind_knowledge_reply
from k3_support.knowledge_runtime import query_knowledge
from k3_support.lark import CommandResult
from k3_support.message_format import format_feishu_ai_message
from k3_support.routing import record_route_decision
from k3_support.store import create_case, enqueue_outbox


def queued_fixture(conn, config, tmp_path, monkeypatch):
    cfg, payload, policy, observed, write = signed_fixture(
        conn, config, tmp_path, monkeypatch
    )
    cfg.raw["mode"] = "active"
    cfg.raw["features"]["auto_faq"] = True
    cfg.raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    question = "K3 U-Boot 里怎么确认 UFS 是否识别？现场使用 K3，已经进入 U-Boot，存储介质 UFS，版本 commit-1。"
    event_pk, _ = admit_im_event(
        conn,
        cfg,
        {
            "source": "feishu_user_poll",
            "identity": "user",
            "external_id": "om_release_test",
            "payload": {"content": question, "chat_type": "p2p"},
            "occurred_at": "2026-09-07T01:00:00+00:00",
            "sender_id": "ou_fixture",
            "chat_id": "oc_fixture",
        },
    )
    case_id, _ = create_case(
        conn,
        title="Synthetic release delivery",
        case_type="faq",
        severity="P3",
        confidence=1.0,
        requester_id="ou_fixture",
        requester_chat_id="oc_fixture",
        source_event_pk=event_pk,
    )
    conn.execute("UPDATE cases SET state='answering' WHERE case_id=?", (case_id,))
    with transaction(conn):
        ensure_turn(conn, case_id=case_id, source_event_pk=event_pk)
        communication = bind_ai_communication(
            conn, cfg, case_id=case_id, source_event_pk=event_pk
        )
    context = resolve_event_context(conn, event_pk)
    observed = query_knowledge(
        conn,
        query=question,
        requester_id="ou_fixture",
        chat_id="oc_fixture",
        context_binding=context["binding"],
        options=cfg.raw["knowledge_retrieval"],
        selector=observed_fixture_selector,
    )
    record_route_decision(
        conn,
        event_pk=event_pk,
        case_id=case_id,
        profile={},
        knowledge=observed["selected_entry"],
        route={
            "route": "direct_answer",
            "proposed_route": "direct_answer",
            "confidence": 1.0,
            "issue_type": "faq",
            "severity": "P3",
            "domain": "boot",
            "repository_hints": [],
            "reason_codes": [],
            "requires_owner_judgment": False,
            "model_output_digest": digest(observed),
        },
    )
    text = format_feishu_ai_message(
        approved_answer_markdown(conn, observed["selected_entry"])
    )
    proof = bind_knowledge_reply(
        conn,
        cfg,
        source_event_pk=event_pk,
        knowledge_ids=list(payload["entries"]),
        text=text,
    )
    assert proof["release_digest"], proof
    with transaction(conn):
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="reply",
            destination="om_release_test",
            payload={
                "text": text,
                "identity": "user",
                "reply_basis": "approved_knowledge",
                "knowledge_release": proof,
            },
            idempotency_key="signed-knowledge-reply",
            source_event_pk=event_pk,
            case_id=case_id,
            **{
                key: value
                for key, value in communication.items()
                if key != "not_before"
            },
        )
    monkeypatch.setattr(
        "k3_support.ingress.poll_operator_activity", lambda *args, **kwargs: {}
    )
    return cfg, payload, policy, observed, write, outbox_id


def test_valid_exact_release_passes_actual_outbox_transport(
    conn, config, tmp_path, monkeypatch
):
    cfg, _, _, _, _, outbox_id = queued_fixture(conn, config, tmp_path, monkeypatch)
    calls = []
    row = delivery.claim_outbox(conn, worker_id="fixture-sender")
    receipt = delivery.deliver_claimed(
        conn,
        cfg,
        row,
        lark_runner=lambda argv: (
            calls.append(argv)
            or CommandResult({"message_id": "om_valid_receipt"}, "user", [])
        ),
    )
    assert len(calls) == 1 and calls[0][-2:] == ["--as", "user"]
    assert receipt.remote_id == "om_valid_receipt"
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()[0]
        == "delivered"
    )


def test_actual_inbound_to_signed_answer_and_delivery_receipt(conn, config, tmp_path, monkeypatch):
    from test_routing import route_value

    from k3_support.orchestrator import process_inbound
    from k3_support.routing import set_requester_profile
    from k3_support.timeutil import iso_now

    cfg, _, _, _, _ = signed_fixture(conn, config, tmp_path, monkeypatch)
    cfg.raw['mode'] = 'active'
    cfg.raw['features']['auto_faq'] = True
    set_requester_profile(conn, requester_id='ou_fixture', relationship='peer',
        function_role='engineering', source='operator', relationship_confidence=1., function_confidence=1.)
    event_pk, _ = admit_im_event(conn, cfg, {
        'source': 'feishu_user_poll', 'identity': 'user', 'external_id': 'om_full_path',
        'occurred_at': iso_now(), 'sender_id': 'ou_fixture', 'chat_id': 'oc_fixture',
        'payload': {'chat_type': 'p2p', 'content':
            'K3 U-Boot 里怎么确认 UFS 是否识别？现场使用 K3，已经进入 U-Boot，存储介质 UFS，版本 commit-1。'},
    })
    outcome = process_inbound(conn, event_pk=event_pk, worker_id='fixture-router', config=cfg,
        semantic_selector=observed_fixture_selector,
        message_router=lambda _: route_value('direct_answer'))
    assert outcome['route']['route'] == 'direct_answer', outcome
    pending = conn.execute("SELECT not_before FROM outbox WHERE action_type='reply'").fetchone()
    assert pending is not None, outcome
    if pending['not_before']:
        from datetime import datetime, timedelta

        class DueClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return (datetime.fromisoformat(pending['not_before']) + timedelta(seconds=1)).astimezone(tz)

        monkeypatch.setattr(delivery, 'datetime', DueClock)
    row = delivery.claim_outbox(conn, worker_id='fixture-sender',
        eligible=lambda item: item['action_type'] == 'reply')
    assert row is not None
    monkeypatch.setattr('k3_support.ingress.poll_operator_activity', lambda *args, **kwargs: {})
    calls = []
    receipt = delivery.deliver_claimed(conn, cfg, row, lark_runner=lambda argv:
        calls.append(argv) or CommandResult({'message_id': 'om_full_path_receipt'}, 'user', []))
    assert receipt.remote_id == 'om_full_path_receipt'
    assert len(calls) == 1
    assert conn.execute('SELECT state FROM outbox WHERE outbox_id=?',
        (row['outbox_id'],)).fetchone()[0] == 'delivered'


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "legacy",
        "dynamic_link",
        "content",
        "revoked",
        "wrong_scope",
        "fallback",
        "masquerade",
        "query",
        "sender",
        "chat",
    ],
)
def test_invalid_release_never_calls_transport_even_with_auto_enabled(
    conn, config, tmp_path, monkeypatch, change
):
    cfg, payload, policy, _, _, outbox_id = queued_fixture(
        conn, config, tmp_path, monkeypatch
    )
    saved = conn.execute(
        "SELECT payload_json,source_event_pk FROM outbox WHERE outbox_id=?",
        (outbox_id,),
    ).fetchone()
    message = json.loads(saved["payload_json"])
    if change == "missing":
        cfg.raw["knowledge_release"]["artifact_path"] = None
    elif change in {"legacy", "dynamic_link"}:
        message.pop("knowledge_release")
        message["reply_basis"] = (
            "verified_link_route" if change == "dynamic_link" else "approved_knowledge"
        )
    elif change == "content":
        conn.execute("UPDATE knowledge_entries SET answer_markdown='different command'")
    elif change == "revoked":
        policy["revoked_release_ids"].append(payload["release_id"])
    elif change == "masquerade":
        message.pop("knowledge_release")
        message["reply_basis"] = "verified_evidence"
        cfg.raw["features"]["codex"] = True
        cfg.raw["knowledge_release"]["artifact_path"] = None
    elif change == "query":
        conn.execute(
            "UPDATE inbound_events SET payload_json=? WHERE event_pk=?",
            (
                canonical_json(
                    {"content": "应该烧录哪个 EC 固件？", "chat_type": "p2p"}
                ),
                saved["source_event_pk"],
            ),
        )
    elif change in {"sender", "chat"}:
        column = "sender_id" if change == "sender" else "chat_id"
        conn.execute(
            f"UPDATE inbound_events SET {column}='different' WHERE event_pk=?",
            (saved["source_event_pk"],),
        )
    elif change in {"wrong_scope", "fallback"}:
        provenance = message["knowledge_release"]["provenance"]
        if change == "wrong_scope":
            provenance["knowledge_observed_scope"]["software_version"] = "wrong-version"
        else:
            provenance["knowledge_runtime_binding"]["effective_backend"] = "unavailable"
        conn.execute(
            "UPDATE route_decisions SET knowledge_runtime_json=? WHERE event_pk=?",
            (canonical_json(provenance), saved["source_event_pk"]),
        )
    conn.execute(
        "UPDATE outbox SET payload_json=? WHERE outbox_id=?",
        (canonical_json(message), outbox_id),
    )
    row = delivery.claim_outbox(conn, worker_id="fixture-sender")
    if change in {"query", "sender", "chat"}:
        # Current-context integrity now rejects these before a transport claim
        # exists. This is earlier fencing, not a successful/ignored send.
        assert row is None
        cancelled = conn.execute(
            "SELECT state,suppression_reason FROM outbox WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        assert (
            cancelled["state"] == "cancelled"
            and "context" in cancelled["suppression_reason"]
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM outbox_attempts WHERE outbox_id=?", (outbox_id,)
            ).fetchone()[0]
            == 0
        )
        return
    with pytest.raises(delivery.DeliverySuppressed, match="knowledge_release"):
        delivery.deliver_claimed(
            conn, cfg, row, lark_runner=lambda _: pytest.fail("invalid release sent")
        )
    result = conn.execute(
        "SELECT state,suppression_reason FROM outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()
    assert result[0] == "cancelled" and "knowledge_release" in result[1]
    assert (
        conn.execute(
            "SELECT dispatch_started_at FROM outbox_attempts WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()[0]
        is None
    )


def test_revocation_between_precheck_and_dispatch_still_sends_nothing(
    conn, config, tmp_path, monkeypatch
):
    cfg, payload, policy, _, _, _ = queued_fixture(conn, config, tmp_path, monkeypatch)
    original = delivery._begin_dispatch

    def revoke(*args):
        policy["revoked_release_ids"].append(payload["release_id"])
        return original(*args)

    monkeypatch.setattr(delivery, "_begin_dispatch", revoke)
    with pytest.raises(delivery.DeliverySuppressed, match="release_revoked"):
        delivery.deliver_claimed(
            conn,
            cfg,
            delivery.claim_outbox(conn, worker_id="sender"),
            lark_runner=lambda _: pytest.fail("revoked at dispatch"),
        )


@pytest.mark.parametrize("phase", ["claimed", "dispatch"])
def test_correction_after_selection_and_claim_never_sends_old_context_answer(
    conn, config, tmp_path, monkeypatch, phase
):
    cfg, _, _, _, _, outbox_id = queued_fixture(conn, config, tmp_path, monkeypatch)
    claimed = delivery.claim_outbox(conn, worker_id="context-race-sender")

    def correction():
        admit_im_event(
            conn,
            cfg,
            {
                "source": "feishu_user_poll",
                "identity": "user",
                "external_id": "om_new_scope",
                "sender_id": "ou_fixture",
                "chat_id": "oc_fixture",
                "occurred_at": "2026-09-07T02:00:00+00:00",
                "payload": {
                    "content": "纠正，尚未进入 U-Boot",
                    "chat_type": "p2p",
                    "parent_id": "om_release_test",
                },
            },
        )

    if phase == "claimed":
        correction()
    else:
        begin = delivery._begin_dispatch

        def corrected_begin(*args):
            correction()
            return begin(*args)

        monkeypatch.setattr(delivery, "_begin_dispatch", corrected_begin)
    with pytest.raises(delivery.DeliverySuppressed, match="context"):
        delivery.deliver_claimed(
            conn,
            cfg,
            claimed,
            lark_runner=lambda *_: pytest.fail("old context was sent"),
        )
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()[0]
        == "cancelled"
    )
    assert (
        conn.execute(
            "SELECT dispatch_started_at FROM outbox_attempts WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()[0]
        is None
    )


def test_legacy_missing_context_is_rejected_not_backfilled(
    conn, config, tmp_path, monkeypatch
):
    _cfg, _, _, _, _, outbox_id = queued_fixture(conn, config, tmp_path, monkeypatch)
    conn.execute(
        "UPDATE outbox SET context_id=NULL,context_revision=NULL,context_digest=NULL WHERE outbox_id=?",
        (outbox_id,),
    )
    assert delivery.claim_outbox(conn, worker_id="legacy-sender") is None
    row = conn.execute(
        "SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()
    assert row["state"] == "cancelled" and row["context_id"] is None
