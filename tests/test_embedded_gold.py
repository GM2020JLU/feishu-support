from __future__ import annotations

import copy
import hashlib
import json

import pytest

from k3_support import knowledge_gold_review as review
from k3_support.ids import canonical_json


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _jsonl(items) -> bytes:
    return "".join(canonical_json(item) + "\n" for item in items).encode()


def _seal(manifest, reviews, gold):
    manifest["gold_digest"] = _hash(_jsonl(gold))
    manifest["review_digest"] = _hash(_jsonl(reviews))
    return _hash((canonical_json(manifest) + "\n").encode())


@pytest.fixture
def evidence():
    batch = "a" * 64
    gold, reviews, cases = [], [], []
    for index in range(3):
        case_id = f"candidate-{index:024x}"
        query = f"Question {index}: UFS inspection"
        label = {
            "expected_route": "direct_answer", "answerable": True,
            "allowed_knowledge_ids": [f"article-{index}"], "allowed_claim_ids": ["inspect"],
            "forbidden_knowledge_ids": [], "required_scope": {"product": "K3"},
            "clarification_allowed": False, "acceptable_abstention_reasons": [],
            "tags": ["human_reviewed", "synthetic", "positive"],
        }
        reviews.append({
            "schema_version": 1, "candidate_id": case_id, "candidate_digest": batch,
            "query_digest": _hash(query.encode()), "decision": "accepted" if index < 2 else "rejected",
            "reviewed_by": "fixture-reviewer", "reviewed_at": "2026-09-01T12:00:00+05:45",
            "notes": "synthetic test evidence, not production authority",
            "label": copy.deepcopy(label) if index < 2 else None,
        })
        if index < 2:
            gold.append({"id": case_id, "query": query, **label})
            cases.append({"id": case_id, "reviewed_by": "fixture-reviewer",
                          "reviewed_at": reviews[-1]["reviewed_at"],
                          "source_type": "approved_knowledge", "source_id": f"fixture-{index}"})
    manifest = {"schema_version": 1, "candidate_digest": batch, "candidate_count": 3,
                "reviewed_count": 3, "accepted_count": 2, "rejected_count": 1,
                "unreviewed_count": 0, "complete": True, "reviewers": ["fixture-reviewer"],
                "reviewed_at_min": reviews[0]["reviewed_at"], "reviewed_at_max": reviews[0]["reviewed_at"],
                "cases": cases}
    return manifest, reviews, gold, _seal(manifest, reviews, gold)


def _file_bundle(tmp_path, manifest, reviews, gold):
    root = tmp_path / "gold"
    root.mkdir(mode=0o700)
    for name, value in (("manifest.json", (canonical_json(manifest) + "\n").encode()),
                        ("reviews.jsonl", _jsonl(reviews)), ("gold.jsonl", _jsonl(gold))):
        path = root / name
        path.write_bytes(value)
        path.chmod(0o600)
    return root


def test_embedded_is_exactly_the_file_core_and_never_writes(evidence, tmp_path, monkeypatch):
    manifest, reviews, gold, approved = evidence
    root = _file_bundle(tmp_path, manifest, reviews, gold)
    file_verification = review.verify_gold_bundle(root, expected_manifest_digest=approved)
    before = copy.deepcopy(evidence)

    def forbidden(*args, **kwargs):
        raise AssertionError("embedded evidence must not become a temporary file bundle")

    monkeypatch.setattr(review, "_private_regular_file", forbidden)
    monkeypatch.setattr(review.tempfile, "mkstemp", forbidden)
    result = review.verify_embedded_gold_bundle(manifest, reviews, gold, approved)
    assert result == {key: value for key, value in file_verification.items() if key != "output"}
    assert result["complete"] and result["accepted_count"] == 2
    assert evidence == before


@pytest.mark.parametrize("mutation,reason", [
    ("query", "query digest"), ("batch", "candidate batch digest"),
    ("model_tags", "proposal-only"), ("missing_human_review", "human_reviewed tag"),
    ("label", "label does not match"), ("count", "accepted_count"),
    ("reviewer", "review identity"), ("time", "reviewed_at_max"),
    ("review_order", "reviews are not canonical"), ("gold_order", "cases are not canonical"),
    ("duplicate_review", "duplicate bundled review"), ("missing_review", "accepted review IDs"),
    ("hidden_pending", "review rows exceed"),
])
def test_recomputed_outer_hashes_cannot_hide_broken_internal_bindings(evidence, tmp_path, mutation, reason):
    manifest, reviews, gold, _ = evidence
    if mutation == "query":
        gold[0]["query"] = "A different question nobody reviewed"
    elif mutation == "batch":
        reviews[0]["candidate_digest"] = "b" * 64
    elif mutation == "model_tags":
        for value in (reviews[0]["label"], gold[0]):
            value["tags"].append("model_suggestion_unreviewed")
    elif mutation == "missing_human_review":
        for value in (reviews[0]["label"], gold[0]):
            value["tags"].remove("human_reviewed")
    elif mutation == "label":
        reviews[0]["label"]["allowed_claim_ids"] = ["unreviewed"]
    elif mutation == "count":
        manifest["accepted_count"] += 1
    elif mutation == "reviewer":
        manifest["cases"][0]["reviewed_by"] = "other-reviewer"
    elif mutation == "time":
        manifest["reviewed_at_max"] = "2026-09-02T12:00:00+05:45"
    elif mutation == "review_order":
        reviews.reverse()
    elif mutation == "gold_order":
        gold.reverse()
    elif mutation == "duplicate_review":
        reviews.append(copy.deepcopy(reviews[-1]))
    elif mutation == "missing_review":
        reviews.pop(0)
    elif mutation == "hidden_pending":
        reviews.append({**copy.deepcopy(reviews[-1]), "candidate_id": "candidate-ffffffffffffffffffffffff",
                        "decision": "pending", "reviewed_by": None, "reviewed_at": None})
    approved = _seal(manifest, reviews, gold)
    root = _file_bundle(tmp_path, manifest, reviews, gold)
    for check in (
        lambda: review.verify_embedded_gold_bundle(manifest, reviews, gold, approved),
        lambda: review.verify_gold_bundle(root, expected_manifest_digest=approved),
    ):
        with pytest.raises(review.KnowledgeGoldReviewError, match=reason):
            check()


def test_verified_partial_evidence_is_not_mislabelled_complete(evidence):
    manifest, reviews, gold, _ = evidence
    manifest.update(candidate_count=4, unreviewed_count=1, complete=False)
    approved = _seal(manifest, reviews, gold)
    assert review.verify_embedded_gold_bundle(manifest, reviews, gold, approved)["complete"] is False


@pytest.mark.parametrize("approved", [None, "", "a" * 63, "G" * 64, "0" * 64])
def test_embedded_requires_the_exact_approved_digest(evidence, approved):
    manifest, reviews, gold, _ = evidence
    with pytest.raises(review.KnowledgeGoldReviewError, match="digest"):
        review.verify_embedded_gold_bundle(manifest, reviews, gold, approved)


def test_embedded_preserves_input_and_line_size_limits(evidence, monkeypatch):
    manifest, reviews, gold, approved = evidence
    with monkeypatch.context() as patch:
        patch.setattr(review, "_MAX_INPUT_BYTES", 10)
        with pytest.raises(review.KnowledgeGoldReviewError, match="100 MiB"):
            review.verify_embedded_gold_bundle(manifest, reviews, gold, approved)
    with monkeypatch.context() as patch:
        patch.setattr(review, "_MAX_LINE_BYTES", 10)
        with pytest.raises(review.KnowledgeGoldReviewError, match="1 MiB"):
            review.verify_embedded_gold_bundle(manifest, reviews, gold, approved)


def test_file_api_still_requires_private_canonical_raw_bytes(evidence, tmp_path):
    manifest, reviews, gold, approved = evidence
    root = _file_bundle(tmp_path, manifest, reviews, gold)
    path = root / "manifest.json"
    path.chmod(0o644)
    with pytest.raises(review.KnowledgeGoldReviewError, match="group/world"):
        review.verify_gold_bundle(root, expected_manifest_digest=approved)
    path.chmod(0o600)
    path.write_text(json.dumps(manifest, indent=2))
    with pytest.raises(review.KnowledgeGoldReviewError, match="not canonical"):
        review.verify_gold_bundle(root, expected_manifest_digest=approved)


@pytest.mark.parametrize("field,value", [(0, []), (1, "not-list"), (2, ["not-object"])])
def test_invalid_embedded_containers_fail_cleanly(evidence, field, value):
    values = list(evidence)
    values[field] = value
    with pytest.raises(review.KnowledgeGoldReviewError, match="container types"):
        review.verify_embedded_gold_bundle(*values)
