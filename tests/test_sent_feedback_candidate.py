import json

import pytest

from k3_support.knowledge_gold import candidate_from_sent_feedback


def material(**changes):
    return {"question": "Pico 风扇怎么调 token=private-value", "question_available": True,
            "feedback_id": "feedback", "material_digest": "a" * 64,
            "source_event_digest": "b" * 64, "sent_entry_fingerprint": "c" * 64,
            "sent_answer": "UNVERIFIED OLD ANSWER", **changes}


def test_feedback_candidate_is_deterministic_private_and_unreviewed():
    source = material()
    candidate = candidate_from_sent_feedback(source)
    assert candidate == candidate_from_sent_feedback(source)
    assert "private-value" not in str(candidate)
    assert "UNVERIFIED OLD ANSWER" not in str(candidate)
    assert candidate["status"] == "candidate"
    assert candidate["review"]["decision"] is None
    assert "requires_human_scope_review" in candidate["suggestion"]["tags"]
    assert not candidate["suggestion"]["allowed_knowledge_ids"]
    assert candidate["id"] != candidate_from_sent_feedback(material(material_digest="d" * 64))["id"]


@pytest.mark.parametrize("changes", [
    {"question": None}, {"question": "x"}, {"question": "x" * 10001},
    {"question_available": False},
])
def test_missing_or_invalid_question_does_not_invent_candidate(changes):
    assert candidate_from_sent_feedback(material(**changes)) is None


def test_candidate_enters_existing_review_flow_but_is_not_gold(tmp_path):
    from k3_support.knowledge_eval import KnowledgeEvaluationError, load_gold_set
    from k3_support.knowledge_gold_review import initialize_gold_reviews

    candidates = tmp_path / "candidates.jsonl"
    candidates.write_text(json.dumps(candidate_from_sent_feedback(material())) + "\n")
    candidates.chmod(0o600)
    reviews = tmp_path / "reviews.jsonl"
    result = initialize_gold_reviews(candidates_path=candidates, output_path=reviews)
    assert result["review_count"] == 1
    row = json.loads(reviews.read_text())
    assert row["decision"] == "pending" and row["reviewed_by"] is None
    assert "route_not_adjudicated" in row["label"]["tags"]
    with pytest.raises(KnowledgeEvaluationError):
        load_gold_set(candidates)
