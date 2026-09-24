from __future__ import annotations

import pytest

from k3_support.evidence import record_knowledge_evidence
from k3_support.knowledge import (
    create_candidate,
    record_delivered_use,
    record_feedback,
    review,
)
from k3_support.store import create_case, enqueue_outbox


def approved_knowledge(conn, case_id):
    knowledge_id = create_candidate(
        conn,
        title="Pico 风扇控制",
        questions=["怎么调风扇"],
        answer_markdown="查看维护文档",
        project="K3",
        module="EC",
        software_version=None,
        disclosure_class="internal",
        confidence=0.95,
        source_authority=0.95,
        source_digest="source-feedback",
        canonical_case_id=case_id,
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    return knowledge_id


def test_feedback_cannot_attach_unrelated_knowledge_to_case(conn):
    from k3_support.knowledge import KnowledgeError

    case_id, _ = create_case(conn, title="unrelated", case_type="faq", severity="P3", confidence=0.9)
    knowledge_id = approved_knowledge(conn, case_id)
    before = conn.serialize()
    with pytest.raises(KnowledgeError, match="no delivered use"):
        record_feedback(conn, verdict="incorrect", actor_id="owner",
                        case_id=case_id, knowledge_id=knowledge_id)
    assert conn.serialize() == before


def test_delivered_knowledge_use_is_counted_once_and_feedback_self_heals(conn, config):
    case_id, _ = create_case(
        conn, title="fan", case_type="faq", severity="P3", confidence=0.95
    )
    knowledge_id = approved_knowledge(conn, case_id)
    knowledge = dict(
        conn.execute(
            "SELECT * FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
        ).fetchone()
    )
    record_knowledge_evidence(conn, case_id=case_id, knowledge=knowledge)
    outbox_id, _ = enqueue_outbox(
        conn,
        channel="feishu_im",
        action_type="reply",
        destination="om_question",
        payload={"text": "[AI 自动回复] answer", "identity": "user"},
        idempotency_key="knowledge-feedback-reply",
        case_id=case_id,
    )
    # Historical pre-release receipts remain countable, but old status-only
    # knowledge is no longer authorized to send a new reply.
    assert record_delivered_use(conn, case_id=case_id, outbox_id=outbox_id) is None
    conn.execute("UPDATE outbox SET state='delivered',remote_message_id='om_historical_answer' WHERE outbox_id=?", (outbox_id,))
    record_delivered_use(conn, case_id=case_id, outbox_id=outbox_id)

    assert conn.execute(
        "SELECT use_count FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()[0] == 1
    record_delivered_use(conn, case_id=case_id, outbox_id=outbox_id)
    assert conn.execute(
        "SELECT use_count FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()[0] == 1

    helpful = record_feedback(
        conn,
        verdict="helpful",
        actor_id="owner",
        case_id=case_id,
        detail="confirmed",
    )
    duplicate = record_feedback(
        conn,
        verdict="helpful",
        actor_id="owner",
        case_id=case_id,
        detail="confirmed",
    )
    assert helpful["created"] is True
    assert duplicate["created"] is False
    assert conn.execute(
        "SELECT success_count FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()[0] == 0  # helpful is not a confirmed field resolution
    assert conn.execute(
        "SELECT count(*) FROM knowledge_feedback WHERE knowledge_id=? AND verdict='helpful'", (knowledge_id,)
    ).fetchone()[0] == 1

    corrected = record_feedback(
        conn,
        verdict="incomplete",
        actor_id="owner",
        case_id=case_id,
        detail="missing EC version boundary",
    )
    assert corrected["status"] == "stale"
    assert tuple(
        conn.execute(
            "SELECT status,correction_count FROM knowledge_entries WHERE knowledge_id=?",
            (knowledge_id,),
        ).fetchone()
    ) == ("stale", 1)


@pytest.mark.parametrize("kind", ["coding_reply", "ambiguous_legacy", "unbound_attempt"])
def test_unattributed_receipt_never_increments_knowledge_usage(conn, kind):
    case_id, _ = create_case(conn, title="fixture", case_type="bug", severity="P3", confidence=0.9)
    knowledge_id = approved_knowledge(conn, case_id)
    knowledge = dict(conn.execute("SELECT * FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)).fetchone())
    record_knowledge_evidence(conn, case_id=case_id, knowledge=knowledge)
    payload = {"text": "fixture", "identity": "user"}
    if kind == "coding_reply":
        payload["reply_basis"] = "verified_evidence"
    elif kind == "ambiguous_legacy":
        source = conn.execute("SELECT * FROM case_sources WHERE case_id=?", (case_id,)).fetchone()
        conn.execute("""INSERT INTO case_sources(source_id,case_id,source_type,stable_external_id,
                         visibility,requester_access,authority)
                        VALUES('fixture-other-source',?,'approved_knowledge','other-knowledge',?,?,?)""",
                     (case_id, source["visibility"], source["requester_access"], source["authority"]))
    outbox_id, _ = enqueue_outbox(conn, channel="feishu_im", action_type="reply", destination="fixture",
                                  payload=payload, idempotency_key="fixture-receipt", case_id=case_id)
    conn.execute("UPDATE outbox SET state='delivered',remote_message_id='fixture-receipt',claim_token=? WHERE outbox_id=?",
                 ("fixture-token" if kind == "unbound_attempt" else None, outbox_id))
    assert record_delivered_use(conn, case_id=case_id, outbox_id=outbox_id) is None
    assert conn.execute("SELECT use_count FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM knowledge_uses").fetchone()[0] == 0
