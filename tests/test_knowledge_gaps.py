from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from k3_support.db import connect
from k3_support.knowledge import create_candidate, record_feedback
from k3_support.knowledge_gaps import KnowledgeGapError, knowledge_gap_report
from k3_support.store import create_case, enqueue_outbox, ingest_event

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
WHEN = "2026-09-07T09:00:00+00:00"


def article(conn, key, *, status="approved", professional=False, due="2026-09-08T00:00:00+00:00"):
    knowledge_id = create_candidate(conn, title=f"Article {key}", questions=[f"Question {key}"],
                                    answer_markdown="PRIVATE ANSWER MUST NOT BE IN REPORT", project="K3", module="u-boot",
                                    software_version=None, disclosure_class="internal", confidence=0.9,
                                    source_authority=0.9, canonical_case_id=None, source_digest=f"source-{key}")
    conn.execute("UPDATE knowledge_entries SET status=?,review_due_at=? WHERE knowledge_id=?", (status, due, knowledge_id))
    if professional:
        revision = f"revision-{key}"
        conn.execute("""INSERT INTO professional_knowledge_revisions
                     (revision_id,stable_id,revision_number,knowledge_id,kind,lifecycle_state,revision_digest,
                      payload_json,body_markdown,owner,reviewed_by,reviewed_at,review_due_at,imported_at)
                     VALUES(?,?,1,?,'procedure','published',?,'{}','private body','fixture-owner','fixture-reviewer',?,?,?)""",
                     (revision, key, knowledge_id, f"digest-{key}", WHEN, due, WHEN))
        conn.execute("UPDATE knowledge_entries SET professional_revision_id=? WHERE knowledge_id=?", (revision, knowledge_id))
    return knowledge_id


def question(conn, index, text="pico怎么调风扇", *, when=WHEN, route="research", knowledge_id=None, case_id=None):
    event, _ = ingest_event(conn, source="feishu_user_poll", identity="user", external_id=f"message-{index}",
                            payload={"text": text}, occurred_at=when)
    if case_id is None:
        case_id, _ = create_case(conn, title=text, case_type="faq", severity="P2", confidence=0.8, source_event_pk=event)
    conn.execute("""INSERT INTO route_decisions
                    (route_decision_id,event_pk,case_id,route,proposed_route,confidence,issue_type,severity,domain,
                     requires_owner_judgment,profile_snapshot_json,model_output_digest,created_at,knowledge_id)
                    VALUES(?,?,?,?,?,0.9,'faq','P2','boot',0,'{}','fixture',?,?)""",
                 (f"route-{index}", event, case_id, route, route, when, knowledge_id))
    return case_id, event


def delivered(conn, case_id, event, key, *, when=WHEN):
    outbox, _ = enqueue_outbox(conn, channel="feishu_im", action_type="reply", destination="fixture-message",
                               payload={"text": "reply"}, idempotency_key=f"receipt-{key}", case_id=case_id, source_event_pk=event)
    conn.execute("UPDATE outbox SET state='delivered',remote_message_id=?,delivered_at=? WHERE outbox_id=?", (f"remote-{key}", when, outbox))
    return outbox


def report(conn, **kwargs):
    return knowledge_gap_report(conn, now=NOW, **kwargs)


def test_repeated_intents_are_candidates_not_truth_or_one_case_spam(conn):
    question(conn, 1, "Pico 风扇怎么调？")
    question(conn, 2, "请问，pico 如何调风扇")
    same, _ = question(conn, 3, "UFS v1 分区查询")
    question(conn, 4, "UFS v1 分区查询", case_id=same)
    question(conn, 5, "UFS v2 分区查询")
    ignored, _ = question(conn, 6, "Pico 风扇怎么调", route="ignore")
    answered, event = question(conn, 7, "Pico 风扇怎么调", route="direct_answer")
    delivered(conn, answered, event, 7)
    result = report(conn)
    candidates = result["repeated_intents"]
    assert candidates["total"] == 1
    assert candidates["items"][0]["distinct_case_count"] == 2
    assert candidates["items"][0]["human_truth"] is False
    assert candidates["items"][0]["status"] == "unreviewed_candidate"
    assert ignored not in candidates["items"][0]["case_ids_preview"]
    assert result["coverage"]["exact_linked_ai_reply_received"] == {
        "numerator": 1, "denominator": 6, "rate": 1 / 6, "unit": "nonignored routed chat question events"}


def test_pagination_keeps_exact_totals_and_stable_digest(conn):
    for index, text in enumerate(("UFS查询介质", "UFS查询介质", "EC固件更新", "EC固件更新", "SSD启动选择", "SSD启动选择")):
        question(conn, index, text)
    one = report(conn, page=1, page_size=1)
    two = report(conn, page=2, page_size=1, expected_digest=one["report_digest"])
    three = report(conn, page=3, page_size=1, expected_digest=one["report_digest"])
    assert one["repeated_intents"]["total"] == two["repeated_intents"]["total"] == three["repeated_intents"]["total"] == 3
    assert len({value["repeated_intents"]["items"][0]["candidate_id"] for value in (one, two, three)}) == 3
    assert three["repeated_intents"]["has_more"] is False
    assert report(conn, page=20)["repeated_intents"]["items"] == []
    question(conn, 10, "new case")
    with pytest.raises(KnowledgeGapError, match="snapshot changed"):
        report(conn, page=2, expected_digest=one["report_digest"])


def test_outcome_evidence_is_strictly_independent(conn):
    knowledge_id = article(conn, "helpful")
    case, event = question(conn, 1, knowledge_id=knowledge_id, route="direct_answer")
    outbox = delivered(conn, case, event, 1)
    conn.execute("INSERT INTO knowledge_uses VALUES('use',?,?,?,'corrected',?,?)", (knowledge_id, case, outbox, WHEN, WHEN))
    record_feedback(conn, knowledge_id=knowledge_id, case_id=case, verdict="helpful", actor_id="owner", detail="helped")
    record_feedback(conn, knowledge_id=knowledge_id, case_id=case, verdict="incorrect", actor_id="owner", detail="PRIVATE FEEDBACK")
    conn.execute("UPDATE knowledge_feedback SET created_at=?", (WHEN,))
    conn.execute("UPDATE cases SET outcome='awaiting_environment_comparison',outcome_provenance='investigation_review' WHERE case_id=?", (case,))
    legacy, _ = question(conn, 2)
    conn.execute("UPDATE cases SET state='resolved',outcome_provenance='legacy_unknown' WHERE case_id=?", (legacy,))
    closed, _ = question(conn, 3)
    conn.execute("UPDATE cases SET state='resolved',outcome='operator_resolved',outcome_provenance='operator_confirmed' WHERE case_id=?", (closed,))
    result = report(conn)
    outcomes = result["outcomes"]
    assert outcomes["helpful"]["count"] == 1
    assert outcomes["delivered"]["count"] == 1
    assert outcomes["knowledge_delivered_uses"]["count"] == 1  # even after helpful/corrected state updates
    assert outcomes["operator_resolved"]["count"] == 1
    assert outcomes["awaiting_environment_comparison"]["count"] == 1
    assert outcomes["unknown"]["count"] == 1
    for name in ("local_not_reproduced", "field_resolved"):
        assert outcomes[name]["count"] is None and outcomes[name]["evidence_status"] == "not_recorded"
    assert result["feedback"]["negative_feedback_count"] == 1
    assert result["feedback_hotspots"]["items"][0]["negative_feedback_count"] == 1
    assert "PRIVATE FEEDBACK" not in json.dumps(result)


def test_coverage_has_a_nonzero_denominator_and_publication_is_not_release(conn):
    professional = article(conn, "professional", professional=True)
    legacy = article(conn, "legacy")
    question(conn, 1, knowledge_id=professional)
    question(conn, 2, knowledge_id=legacy)
    question(conn, 3)
    result = report(conn)
    assert result["coverage"]["knowledge_selected"]["rate"] == 2 / 3
    assert result["coverage"]["currently_published_professional_selected"]["rate"] == 1 / 3
    assert result["coverage"]["publication_is_not_signed_release_readiness"]
    assert result["coverage"]["not_a_resolution_rate"]


def test_review_queues_are_current_inventory_and_totals_ignore_limit(conn):
    article(conn, "overdue", professional=True, due="2026-09-01T00:00:00+00:00")
    article(conn, "stale", status="stale")
    article(conn, "retired", status="retired", due=None)
    for index in range(3):
        conn.execute("""INSERT INTO source_registry(source_id,source_type,stable_external_id,title,source_version,content_digest,last_checked_at)
                        VALUES(?,'feishu_doc',?,?,NULL,NULL,NULL)""", (f"source-{index}", f"doc-{index}", "source title"))
    conn.execute("""INSERT INTO source_refresh_state(source_id,last_attempt_at,next_attempt_at,failure_count,last_error_type,last_state)
                    VALUES('source-0',?,?,1,'protocol','failed')""", (WHEN, WHEN))
    result = report(conn, since="2026-09-06T00:00:00+00:00", page_size=1)
    assert result["knowledge_review_queue"]["total"] == 2
    assert result["source_review_queue"]["total"] == 3
    assert "refresh_failed" in result["source_review_queue"]["items"][0]["reasons"]
    assert result["scope"]["knowledge_catalog_entries"] == 3
    assert result["scope"]["question_events"] == 0
    assert result["coverage"]["knowledge_selected"]["rate"] is None


def test_half_open_dates_handle_non_hour_offsets_and_route_review_time(conn):
    question(conn, 1, when="2026-09-07T12:00:00+05:45")  # exact start
    question(conn, 2, when="2026-09-07T13:00:00+05:45")  # exact end
    question(conn, 3, when="2026-09-06T01:00:00+00:00")
    conn.execute("UPDATE route_decisions SET review_status='rejected',reviewed_at='2026-09-07T12:30:00+05:45',proposed_route='direct_answer' WHERE route_decision_id='route-3'")
    result = report(conn, since="2026-09-07T12:00:00+05:45", until="2026-09-07T13:00:00+05:45")
    assert result["scope"]["question_events"] == 1
    assert result["feedback"]["rejected_route_count"] == 1
    assert result["feedback"]["observed_route_override_count"] == 1


def test_read_only_database_and_redacted_bounded_previews(conn):
    for index in range(2):
        question(conn, index, 'pico风扇怎么调 password=secret admin@example.com 10.1.2.3')
    before = "\n".join(conn.iterdump())
    conn.execute("PRAGMA query_only=ON")
    result = report(conn)
    assert result["read_only"] and "\n".join(conn.iterdump()) == before
    text = json.dumps(result, ensure_ascii=False)
    assert "secret" not in text and "admin@example.com" not in text and "10.1.2.3" not in text
    assert all(len(value) <= 240 for value in result["repeated_intents"]["items"][0]["examples"])


def test_report_keeps_one_sqlite_snapshot_during_concurrent_feedback(conn):
    knowledge_id = article(conn, "race")
    question(conn, 1, knowledge_id=knowledge_id)
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    writer = connect(database)

    class Interleaved:
        injected = False

        def execute(self, sql, *args):
            if "FROM source_registry" in sql and not self.injected:
                self.injected = True
                record_feedback(writer, knowledge_id=knowledge_id, verdict="incorrect", actor_id="fixture-owner")
                writer.execute("UPDATE knowledge_feedback SET created_at=?", (WHEN,))
            return conn.execute(sql, *args)

    before = report(Interleaved())
    after = report(conn)
    writer.close()
    assert before["feedback"]["negative_feedback_count"] == 0
    assert after["feedback"]["negative_feedback_count"] == 1
    assert before["report_digest"] != after["report_digest"]


@pytest.mark.parametrize("kwargs", [{"page": 0}, {"page_size": 101}, {"min_repeat": 1},
                                   {"since": "2026-09-01"},
                                   {"since": WHEN, "until": WHEN}])
def test_bad_report_inputs_fail_before_changes(conn, kwargs):
    with pytest.raises(ValueError):
        report(conn, **kwargs)
