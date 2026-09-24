"""Synthetic *structural* review records for communication/transport unit tests.

This is not an independent verifier or production approval. Real review-to-send
integration is tested through run_hermes_review elsewhere; these fixtures keep
Case state/Turn ownership unchanged so individual fencing tests remain focused.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from k3_support.conversation_context import resolve_event_context
from k3_support.coordination import bind_ai_communication, ensure_turn
from k3_support.db import transaction
from k3_support.ids import canonical_json, digest, new_id
from k3_support.message_format import format_feishu_ai_message
from k3_support.store import enqueue_outbox
from k3_support.timeutil import epoch_now, iso_now


def attach_reviewed_reply(conn, outbox_id: str) -> dict:
    assert not conn.in_transaction
    row = dict(
        conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    )
    assert row["channel"] == "feishu_im" and row["action_type"] == "reply"
    assert row["state"] == "pending" and row["claim_token"] is None
    assert row["source_event_pk"] and row["turn_id"] and row["context_id"]
    source_context = resolve_event_context(conn, row["source_event_pk"])
    assert source_context and source_context["context_id"] == row["context_id"]
    case = conn.execute(
        "SELECT * FROM cases WHERE case_id=?", (row["case_id"],)
    ).fetchone()
    payload = json.loads(row["payload_json"])
    now = iso_now()
    event_pk = row["source_event_pk"]
    job_id, review_id, source_id, evidence_id = (
        new_id(prefix) for prefix in ("job", "crv", "src", "evd")
    )
    decision_id = f"synthetic-reviewed-{outbox_id}"
    check = {
        "kind": "git",
        "repo": "u-boot",
        "head_commit": "a" * 40,
        "verified": True,
        "synthetic_fixture": True,
    }
    decision = {
        "decision_id": decision_id,
        "case_id": row["case_id"],
        "expected_case_version": case["version"],
        "intent": "reply",
        "confidence": 0.99,
        "evidence_ids": [evidence_id],
        "reply_draft": payload["text"],
        "proposed_actions": [
            {
                "type": "feishu_reply",
                "source_event_pk": event_pk,
                "source_message_id": row["destination"],
            }
        ],
        "facts": ["Synthetic static check fixture"],
        "inferences": [],
        "unknowns": ["No board/model test performed"],
    }
    payload.update(
        text=format_feishu_ai_message(decision["reply_draft"]),
        format="markdown",
        reply_basis="verified_evidence",
        evidence_ids=[evidence_id],
    )
    with transaction(conn):
        conn.execute(
            """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,output_digest,
            exit_code,available_at,created_at,updated_at,lifecycle_round,context_json)
            VALUES(?,?,'codex','succeeded',?,?,0,?,?,?,?,?)""",
            (
                job_id,
                row["case_id"],
                digest(decision),
                digest(check),
                now,
                now,
                now,
                case["lifecycle_round"],
                canonical_json({"synthetic_fixture": True, "repo": "u-boot"}),
            ),
        )
        conn.execute(
            """INSERT INTO case_sources(source_id,case_id,source_type,stable_external_id,
            source_version,visibility,requester_access,authority,updated_at,metadata_json)
            VALUES(?,?,'codex_remote_git',?,?,'internal','allowed',0.99,?,?)""",
            (
                source_id,
                row["case_id"],
                f"{job_id}:u-boot",
                check["head_commit"],
                now,
                canonical_json(
                    {
                        "job_id": job_id,
                        "synthetic_fixture": True,
                        "reviewed_independently": True,
                    }
                ),
            ),
        )
        conn.execute(
            """INSERT INTO evidence(evidence_id,case_id,source_id,evidence_layer,
            freshness_at,visibility,artifact_hash,claim,result,created_at)
            VALUES(?,?,?,'static',?,'internal',?,'Synthetic static fixture',?,?)""",
            (
                evidence_id,
                row["case_id"],
                source_id,
                now,
                check["head_commit"],
                canonical_json(check),
                now,
            ),
        )
        conn.execute(
            """INSERT INTO codex_reviews(review_id,job_id,case_id,status,result_digest,
            manifest_json,independent_checks_json,evidence_ids_json,hermes_input_digest,
            hermes_output_json,decision_id,created_at,updated_at)
            VALUES(?,?,?,'decision_applied',?,?,?,?,?,?,?,?,?)""",
            (
                review_id,
                job_id,
                row["case_id"],
                digest(check),
                canonical_json({"synthetic_fixture": True}),
                canonical_json([check]),
                canonical_json([evidence_id]),
                digest({"fixture": decision_id}),
                canonical_json(decision),
                decision_id,
                now,
                now,
            ),
        )
        sequence = conn.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?",
            (row["case_id"],),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO case_events(event_id,case_id,sequence,event_type,actor_type,
            source_event_pk,before_state,after_state,detail_json,idempotency_key,created_at,created_epoch)
            VALUES(?,?,?,'decision_applied','hermes',?,?,?,?,?,?,?)""",
            (
                new_id("cev"),
                row["case_id"],
                sequence,
                event_pk,
                case["state"],
                case["state"],
                canonical_json({"decision": decision, "outbox_ids": [outbox_id]}),
                f"decision:{decision_id}",
                now,
                epoch_now(),
            ),
        )
        conn.execute(
            "UPDATE outbox SET payload_json=?,idempotency_key=? WHERE outbox_id=?",
            (
                canonical_json(payload),
                f"decision:{decision_id}:action:0",
                outbox_id,
            ),
        )
    return {
        "review_id": review_id,
        "job_id": job_id,
        "decision_id": decision_id,
        "evidence_ids": [evidence_id],
        "source_event_pk": event_pk,
    }


def enqueue_bound_reviewed_reply(
    conn, config, *, case_id, source_event_pk, destination, payload, idempotency_key
):
    """New-generation fixture only; never backfill an existing Outbox stamp."""
    assert resolve_event_context(conn, source_event_pk)
    with transaction(conn):
        assert ensure_turn(conn, case_id=case_id, source_event_pk=source_event_pk)
        binding = bind_ai_communication(
            conn,
            config,
            case_id=case_id,
            source_event_pk=source_event_pk,
            at=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert binding and binding["context_id"]
        outbox_id, created = enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="reply",
            destination=destination,
            payload=payload,
            idempotency_key=idempotency_key,
            case_id=case_id,
            source_event_pk=source_event_pk,
            **binding,
        )
        assert created
    attach_reviewed_reply(conn, outbox_id)
    return outbox_id
