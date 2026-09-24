from __future__ import annotations

from datetime import UTC, datetime, timedelta

from k3_support.ids import canonical_json, new_id
from k3_support.shadow import report, review_suggestion
from k3_support.store import create_case, ingest_event


def test_shadow_report_and_review_are_auditable(conn):
    now = datetime.now(UTC)
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="shadow-report-message",
        payload={"content": "K3 fastboot", "message_type": "text"},
        occurred_at=now.isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_support",
    )
    case_id, _ = create_case(
        conn,
        title="K3 fastboot",
        case_type="faq",
        severity="P3",
        confidence=0.9,
        requester_id="ou_colleague",
        requester_chat_id="oc_support",
        disclosure_class="internal",
        source_event_pk=event_pk,
        idempotency_key="shadow-report-case",
    )
    suggestion_id = new_id("sug")
    conn.execute(
        """INSERT INTO case_suggestions(suggestion_id,case_id,kind,content_json,confidence,
               evidence_ids_json,policy_version,status,created_at)
           VALUES(?,?,'reply_draft',?,0.96,'[]','test','shadow',?)""",
        (suggestion_id, case_id, canonical_json({"text": "draft"}), now.isoformat()),
    )

    before = report(conn, days=7, now=now)
    assert before["inbound"]["total"] == 1
    assert before["suggestions"]["high_confidence_unreviewed"] == 1
    assert before["gates"]["ready_for_faq"] is False
    assert before["routing"]["reviewed"] == 0

    reviewed = review_suggestion(
        conn,
        suggestion_id=suggestion_id,
        decision="accepted",
        reviewer_id="ou_owner",
        note="matches reviewed answer",
    )
    repeated = review_suggestion(
        conn,
        suggestion_id=suggestion_id,
        decision="accepted",
        reviewer_id="ou_owner",
        note="duplicate delivery",
    )
    assert reviewed["changed"] is True
    assert repeated == {**reviewed, "changed": False}
    event = conn.execute(
        "SELECT actor_type,actor_id,detail_json FROM case_events WHERE event_id=?",
        (reviewed["event_id"],),
    ).fetchone()
    assert tuple(event)[:2] == ("operator", "ou_owner")
    assert "matches reviewed answer" in event["detail_json"]

    after = report(conn, days=7, now=now)
    assert after["suggestions"]["precision"] == 1.0
    assert after["suggestions"]["reviewed"] == 1
    assert after["gates"]["observation_7d"] is False


def test_shadow_observation_gate_uses_full_history_while_counts_use_window(conn):
    now = datetime.now(UTC)
    old_event, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="shadow-old-message",
        payload={"content": "old"},
        occurred_at=(now - timedelta(days=8)).isoformat(),
    )
    conn.execute(
        "UPDATE inbound_events SET received_epoch=? WHERE event_pk=?",
        (int((now - timedelta(days=8)).timestamp()), old_event),
    )
    recent_event, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="shadow-recent-message",
        payload={"content": "recent"},
        occurred_at=now.isoformat(),
    )

    result = report(conn, days=7, now=now)

    assert result["gates"]["observation_7d"] is True
    assert result["inbound"]["total"] == 1
    assert result["window"]["first_event_epoch"] == int(
        (now - timedelta(days=8)).timestamp()
    )
    assert recent_event != old_event
