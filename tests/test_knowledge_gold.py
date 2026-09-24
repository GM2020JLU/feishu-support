from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from k3_support.knowledge import create_candidate, review
from k3_support.knowledge_eval import KnowledgeEvaluationError, load_gold_set
from k3_support.knowledge_gold import generate_gold_candidates
from k3_support.knowledge_gold_review import (
    KnowledgeGoldReviewError,
    evaluate_gold_bundle,
    initialize_gold_reviews,
    promote_reviewed_candidates,
    verify_gold_bundle,
)
from k3_support.store import create_case, ingest_event


def test_gold_candidates_are_private_redacted_and_never_copy_answers(conn, tmp_path):
    knowledge_id = create_candidate(
        conn,
        title="UFS query",
        questions=["UFS 怎么看？ token=super-secret 10.1.2.3"],
        answer_markdown="PRIVATE ANSWER MUST NOT LEAVE DATABASE",
        project="K3",
        module="ufs",
        software_version=None,
        disclosure_class="private",
        confidence=0.9,
        source_authority=0.9,
        canonical_case_id=None,
        source_digest="source",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    output = tmp_path / "private" / "candidates.jsonl"

    result = generate_gold_candidates(conn, output_path=output)
    payload = output.read_text(encoding="utf-8")
    item = json.loads(payload)

    assert result["status"] == "human_review_required"
    assert result["contains_answers"] is False
    assert output.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE ANSWER" not in payload
    assert "super-secret" not in payload
    assert "10.1.2.3" not in payload
    assert item["status"] == "candidate"
    assert item["review"]["decision"] is None
    with pytest.raises(KnowledgeEvaluationError):
        load_gold_set(output)


def test_candidate_generation_supports_read_only_legacy_review_schema(tmp_path):
    database = tmp_path / "legacy.db"
    legacy = sqlite3.connect(database)
    legacy.row_factory = sqlite3.Row
    legacy.execute(
        """CREATE TABLE knowledge_entries(
               knowledge_id TEXT PRIMARY KEY,status TEXT NOT NULL,
               question_variants_json TEXT NOT NULL,answer_markdown TEXT NOT NULL)"""
    )
    legacy.execute(
        "INSERT INTO knowledge_entries VALUES(?,?,?,?)",
        ("knw_legacy", "approved", '["怎么进入 U-Boot？"]', "private answer"),
    )
    legacy.commit()
    legacy.close()

    readonly = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    output = tmp_path / "candidates.jsonl"
    result = generate_gold_candidates(readonly, output_path=output)

    assert result["candidate_count"] == 1
    assert json.loads(output.read_text(encoding="utf-8"))["suggestion"][
        "allowed_knowledge_ids"
    ] == ["knw_legacy"]


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / 'knowledge' / 'articles').exists(),
    reason='Internal article corpus is not part of the public source candidate',
)
def test_candidate_generation_maps_legacy_id_to_professional_article(tmp_path):
    database = tmp_path / "legacy.db"
    legacy = sqlite3.connect(database)
    legacy.row_factory = sqlite3.Row
    legacy.execute(
        """CREATE TABLE knowledge_entries(
               knowledge_id TEXT PRIMARY KEY,status TEXT NOT NULL,
               question_variants_json TEXT NOT NULL,answer_markdown TEXT NOT NULL)"""
    )
    legacy.execute(
        "INSERT INTO knowledge_entries VALUES(?,?,?,?)",
        (
            "knw_63098f6eee1b42b2a9fa5c589f9642e7",
            "approved",
            '["U-Boot 怎么看 UFS？"]',
            "private answer",
        ),
    )
    output = tmp_path / "candidates.jsonl"
    repository = Path(__file__).resolve().parents[1] / "knowledge"

    generate_gold_candidates(
        legacy,
        output_path=output,
        repository_root=repository,
    )
    candidate = json.loads(output.read_text(encoding="utf-8"))

    assert candidate["suggestion"]["allowed_knowledge_ids"] == [
        "k3.uboot.storage.inspect"
    ]
    assert candidate["suggestion"]["allowed_claim_ids"] == ["legacy-answer"]
    assert "professional_mapping_verified" in candidate["suggestion"]["tags"]


def test_shadow_nonanswer_route_becomes_unreviewed_hard_negative_candidate(
    conn, tmp_path
):
    event_pk, _ = ingest_event(
        conn,
        source="feishu_bot_im",
        identity="bot",
        external_id="shadow-question",
        payload={"content": "启动失败，token=secret"},
        occurred_at="2026-09-04T00:00:00+00:00",
        sender_id="user",
        chat_id="chat",
    )
    case_id, _ = create_case(
        conn,
        title="启动失败，token=secret",
        case_type="bug",
        severity="P2",
        confidence=0.8,
        source_event_pk=event_pk,
    )
    conn.execute(
        """INSERT INTO route_decisions(
               route_decision_id,event_pk,case_id,route,proposed_route,confidence,
               issue_type,severity,domain,repository_hints_json,reason_codes_json,
               clarification_question,fallback_route,requires_owner_judgment,
               profile_snapshot_json,model_output_digest,review_status,created_at)
           VALUES('rtd_shadow',?,?,'codex_debug','codex_debug',0.9,'bug','P2','boot',
                  '[]','[]',NULL,NULL,0,'{}',?,'shadow',?)""",
        (event_pk, case_id, "a" * 64, "2026-09-04T00:00:00+00:00"),
    )
    output = tmp_path / "candidates.jsonl"

    result = generate_gold_candidates(conn, output_path=output)
    items = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]
    shadow = next(
        item for item in items if item["provenance"]["source_id"] == "rtd_shadow"
    )

    assert result["hard_negative_count"] == 1
    assert shadow["provenance"]["source_type"] == "shadow_route_decision"
    assert "model_suggestion_unreviewed" in shadow["suggestion"]["tags"]
    assert "secret" not in shadow["query"]


def _review_row(candidate, candidate_digest, *, decision="accepted", label=None):
    if label is None and decision == "accepted":
        label = {
            "expected_route": "direct_answer",
            "answerable": True,
            "allowed_knowledge_ids": ["k3.uboot.storage.inspect"],
            "allowed_claim_ids": ["storage-command-map"],
            "forbidden_knowledge_ids": ["k3.pico.ec.update-document"],
            "required_scope": {"product": "K3", "component": "u-boot"},
            "clarification_allowed": False,
            "acceptable_abstention_reasons": [],
            "tags": ["positive", "human_reviewed"],
        }
    return {
        "schema_version": 1,
        "candidate_id": candidate["id"],
        "candidate_digest": candidate_digest,
        "query_digest": hashlib.sha256(candidate["query"].encode()).hexdigest(),
        "decision": decision,
        "reviewed_by": "owner-open-id",
        "reviewed_at": "2026-09-04T09:00:00+08:00",
        "notes": "checked against the scoped article",
        "label": label,
    }


def _write_private_jsonl(path, items):
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _two_candidate_batch(conn, tmp_path):
    for index, question in enumerate(["UFS 怎么查看？", "EC 怎么更新？"], 1):
        knowledge_id = create_candidate(
            conn,
            title=f"Knowledge {index}",
            questions=[question],
            answer_markdown=f"private answer {index}",
            project="K3",
            module="u-boot",
            software_version=None,
            disclosure_class="internal",
            confidence=0.9,
            source_authority=0.9,
            canonical_case_id=None,
            source_digest=f"source-{index}",
        )
        review(
            conn,
            knowledge_id=knowledge_id,
            reviewer_id="owner",
            decision="approved",
        )
    candidates_path = tmp_path / "candidates.jsonl"
    result = generate_gold_candidates(conn, output_path=candidates_path)
    candidates = [
        json.loads(line)
        for line in candidates_path.read_text(encoding="utf-8").splitlines()
    ]
    return candidates_path, candidates, result["content_digest"]


def test_reviewed_candidates_promote_to_private_immutable_gold_bundle(conn, tmp_path):
    candidates_path, candidates, candidate_digest = _two_candidate_batch(conn, tmp_path)
    reviews_path = tmp_path / "reviews.jsonl"
    _write_private_jsonl(
        reviews_path,
        [
            _review_row(candidates[0], candidate_digest),
            _review_row(
                candidates[1], candidate_digest, decision="rejected", label=None
            ),
        ],
    )
    output = tmp_path / "gold-v1"

    result = promote_reviewed_candidates(
        candidates_path=candidates_path,
        reviews_path=reviews_path,
        output_dir=output,
    )
    gold = load_gold_set(output / "gold.jsonl")
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    assert result["status"] == "created"
    assert result["accepted_count"] == 1
    assert result["rejected_count"] == 1
    assert result["complete"] is True
    assert gold[0]["allowed_claim_ids"] == ["storage-command-map"]
    assert manifest["candidate_digest"] == candidate_digest
    assert manifest["cases"][0]["reviewed_by"] == "owner-open-id"
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())
    assert "private answer" not in (output / "gold.jsonl").read_text(encoding="utf-8")

    unchanged = promote_reviewed_candidates(
        candidates_path=candidates_path,
        reviews_path=reviews_path,
        output_dir=output,
    )
    assert unchanged["status"] == "unchanged"

    changed_reviews = [
        _review_row(candidates[0], candidate_digest),
        _review_row(candidates[1], candidate_digest, decision="rejected"),
    ]
    changed_reviews[0]["notes"] = "a different review record"
    _write_private_jsonl(reviews_path, changed_reviews)
    with pytest.raises(KnowledgeGoldReviewError, match="refusing to overwrite"):
        promote_reviewed_candidates(
            candidates_path=candidates_path,
            reviews_path=reviews_path,
            output_dir=output,
        )


def test_review_initializer_is_private_digest_bound_and_pending(conn, tmp_path):
    candidates_path, candidates, candidate_digest = _two_candidate_batch(conn, tmp_path)
    output = tmp_path / "reviews.jsonl"

    result = initialize_gold_reviews(
        candidates_path=candidates_path,
        output_path=output,
    )
    reviews = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]

    assert result["status"] == "created"
    assert result["candidate_digest"] == candidate_digest
    assert result["review_count"] == len(candidates)
    assert output.stat().st_mode & 0o777 == 0o600
    assert all(item["decision"] == "pending" for item in reviews)
    assert all(item["reviewed_by"] is None for item in reviews)
    assert reviews[0]["candidate_digest"] == candidate_digest
    assert reviews[0]["label"]["forbidden_knowledge_ids"] == []

    unchanged = initialize_gold_reviews(
        candidates_path=candidates_path,
        output_path=output,
    )
    assert unchanged["status"] == "unchanged"

    with pytest.raises(KnowledgeGoldReviewError, match="unreviewed"):
        promote_reviewed_candidates(
            candidates_path=candidates_path,
            reviews_path=output,
            output_dir=tmp_path / "gold",
        )


def test_gold_promotion_requires_explicit_partial_opt_in(conn, tmp_path):
    candidates_path, candidates, candidate_digest = _two_candidate_batch(conn, tmp_path)
    reviews_path = tmp_path / "reviews.jsonl"
    _write_private_jsonl(reviews_path, [_review_row(candidates[0], candidate_digest)])

    with pytest.raises(KnowledgeGoldReviewError, match="unreviewed"):
        promote_reviewed_candidates(
            candidates_path=candidates_path,
            reviews_path=reviews_path,
            output_dir=tmp_path / "complete",
        )

    result = promote_reviewed_candidates(
        candidates_path=candidates_path,
        reviews_path=reviews_path,
        output_dir=tmp_path / "partial",
        allow_partial=True,
    )
    assert result["complete"] is False
    assert result["unreviewed_count"] == 1


def test_gold_promotion_detects_candidate_tampering(conn, tmp_path):
    candidates_path, candidates, candidate_digest = _two_candidate_batch(conn, tmp_path)
    reviews_path = tmp_path / "reviews.jsonl"
    _write_private_jsonl(
        reviews_path,
        [
            _review_row(candidates[0], candidate_digest),
            _review_row(candidates[1], candidate_digest, decision="rejected"),
        ],
    )
    candidates_path.write_text(
        candidates_path.read_text(encoding="utf-8").replace("UFS", "NVMe"),
        encoding="utf-8",
    )
    candidates_path.chmod(0o600)

    with pytest.raises(KnowledgeGoldReviewError, match="batch digest"):
        promote_reviewed_candidates(
            candidates_path=candidates_path,
            reviews_path=reviews_path,
            output_dir=tmp_path / "gold",
        )


def test_gold_promotion_rejects_inconsistent_human_label(conn, tmp_path):
    candidates_path, candidates, candidate_digest = _two_candidate_batch(conn, tmp_path)
    bad_label = {
        **_review_row(candidates[0], candidate_digest)["label"],
        "answerable": False,
        "acceptable_abstention_reasons": ["no_match"],
    }
    reviews_path = tmp_path / "reviews.jsonl"
    _write_private_jsonl(
        reviews_path,
        [
            _review_row(candidates[0], candidate_digest, label=bad_label),
            _review_row(candidates[1], candidate_digest, decision="rejected"),
        ],
    )

    with pytest.raises(KnowledgeGoldReviewError, match="must agree"):
        promote_reviewed_candidates(
            candidates_path=candidates_path,
            reviews_path=reviews_path,
            output_dir=tmp_path / "gold",
        )


def test_gold_promotion_cannot_rubber_stamp_generated_suggestion(conn, tmp_path):
    candidates_path, candidates, candidate_digest = _two_candidate_batch(conn, tmp_path)
    proposed_label = {
        **_review_row(candidates[0], candidate_digest)["label"],
        "tags": ["positive", "human_reviewed", "requires_human_scope_review"],
    }
    reviews_path = tmp_path / "reviews.jsonl"
    _write_private_jsonl(
        reviews_path,
        [
            _review_row(candidates[0], candidate_digest, label=proposed_label),
            _review_row(candidates[1], candidate_digest, decision="rejected"),
        ],
    )

    with pytest.raises(KnowledgeGoldReviewError, match="proposal-only"):
        promote_reviewed_candidates(
            candidates_path=candidates_path,
            reviews_path=reviews_path,
            output_dir=tmp_path / "gold",
        )


def test_gold_promotion_rejects_readable_review_file(conn, tmp_path):
    candidates_path, candidates, candidate_digest = _two_candidate_batch(conn, tmp_path)
    reviews_path = tmp_path / "reviews.jsonl"
    _write_private_jsonl(
        reviews_path,
        [
            _review_row(candidates[0], candidate_digest),
            _review_row(candidates[1], candidate_digest, decision="rejected"),
        ],
    )
    reviews_path.chmod(0o644)

    with pytest.raises(KnowledgeGoldReviewError, match="group/world"):
        promote_reviewed_candidates(
            candidates_path=candidates_path,
            reviews_path=reviews_path,
            output_dir=tmp_path / "gold",
        )


@pytest.mark.parametrize("failure", ["duplicate", "unknown"])
def test_gold_promotion_rejects_unbound_review_rows(conn, tmp_path, failure):
    candidates_path, candidates, candidate_digest = _two_candidate_batch(conn, tmp_path)
    rows = [
        _review_row(candidates[0], candidate_digest),
        _review_row(candidates[1], candidate_digest, decision="rejected"),
    ]
    if failure == "duplicate":
        rows.append(rows[0])
        expected = "duplicate review file id"
    else:
        rows[1] = {
            **rows[1],
            "candidate_id": "candidate-ffffffffffffffffffffffff",
        }
        expected = "unknown candidates"
    reviews_path = tmp_path / "reviews.jsonl"
    _write_private_jsonl(reviews_path, rows)

    with pytest.raises(KnowledgeGoldReviewError, match=expected):
        promote_reviewed_candidates(
            candidates_path=candidates_path,
            reviews_path=reviews_path,
            output_dir=tmp_path / "gold",
        )


def test_verified_gold_bundle_produces_digest_bound_evaluation(conn, tmp_path):
    candidates_path, candidates, candidate_digest = _two_candidate_batch(conn, tmp_path)
    reviews_path = tmp_path / "reviews.jsonl"
    _write_private_jsonl(
        reviews_path,
        [
            _review_row(candidates[0], candidate_digest),
            _review_row(candidates[1], candidate_digest, decision="rejected"),
        ],
    )
    output = tmp_path / "gold"
    promoted = promote_reviewed_candidates(
        candidates_path=candidates_path,
        reviews_path=reviews_path,
        output_dir=output,
    )
    verified = verify_gold_bundle(
        output,
        expected_manifest_digest=promoted["manifest_digest"],
    )
    predictions = tmp_path / "predictions.jsonl"
    _write_private_jsonl(
        predictions,
        [
            {
                "id": candidates[0]["id"],
                "route": "direct_answer",
                "answered": True,
                "retrieved_knowledge_ids": ["k3.uboot.storage.inspect"],
                "selected_knowledge_id": "k3.uboot.storage.inspect",
                "claim_ids": ["storage-command-map"],
                "abstention_reason": None,
                "resolved_scope": {"product": "K3", "component": "u-boot"},
            }
        ],
    )

    result = evaluate_gold_bundle(
        bundle_dir=output,
        predictions_path=predictions,
        approved_manifest_digest=promoted["manifest_digest"],
    )

    assert verified["status"] == "verified"
    assert verified["manifest_digest"] == promoted["manifest_digest"]
    assert result["trusted_gold_bundle"] is True
    assert result["evaluation"]["metrics"]["sample_size"] == 1
    assert result["evaluation"]["ready_for_automatic_reply"] is False
    baseline_path = tmp_path / "baseline.jsonl"
    baseline_rows = [json.loads(predictions.read_text(encoding="utf-8"))]
    _write_private_jsonl(baseline_path, baseline_rows)
    baseline_rows[0]["resolved_scope"]["component"] = "edk2"
    _write_private_jsonl(predictions, baseline_rows)
    comparison = evaluate_gold_bundle(
        bundle_dir=output,
        predictions_path=predictions,
        approved_manifest_digest=promoted["manifest_digest"],
        baseline_predictions_path=baseline_path,
    )
    assert comparison["comparison"]["regressions"][0]["id"] == candidates[0]["id"]
    assert (
        comparison["baseline_prediction_digest"]
        == hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    )
    assert (
        comparison["baseline_evaluation"]["metrics"]["direct_answer_precision"] == 1.0
    )
    assert comparison["evaluation"]["metrics"]["direct_answer_precision"] == 0.0
    with pytest.raises(KnowledgeGoldReviewError, match="does not match approval"):
        evaluate_gold_bundle(
            bundle_dir=output,
            predictions_path=predictions,
            approved_manifest_digest="0" * 64,
        )
    predictions.chmod(0o644)
    with pytest.raises(KnowledgeGoldReviewError, match="group/world"):
        evaluate_gold_bundle(
            bundle_dir=output,
            predictions_path=predictions,
            approved_manifest_digest=promoted["manifest_digest"],
        )


def test_evaluation_checks_split_before_loading_predictions(conn, tmp_path):
    candidates_path, candidates, candidate_digest = _two_candidate_batch(conn, tmp_path)
    reviews = tmp_path / 'reviews.jsonl'
    _write_private_jsonl(reviews, [_review_row(item, candidate_digest) for item in candidates])
    output = tmp_path / 'gold'
    promoted = promote_reviewed_candidates(candidates_path=candidates_path,
        reviews_path=reviews, output_dir=output)
    plan_path = tmp_path / 'split.json'
    plan = {'schema_version': 1, 'candidate_digest': candidate_digest,
            'assignments': {item['id']: 'tuning' for item in candidates}}
    plan_path.write_text(json.dumps(plan))
    plan_path.chmod(0o600)
    kwargs = dict(bundle_dir=output, predictions_path=tmp_path / 'missing-predictions',
                  approved_manifest_digest=promoted['manifest_digest'], candidates_path=candidates_path)
    with pytest.raises(ValueError, match='supplied together'):
        evaluate_gold_bundle(**kwargs)
    with pytest.raises(ValueError, match='acceptance split'):
        evaluate_gold_bundle(**kwargs, split_plan_path=plan_path)
    plan['assignments'] = {item['id']: 'acceptance' for item in candidates}
    plan_path.write_text(json.dumps(plan))
    # Valid partition advances to the actual prediction loader, not a mock scorer.
    with pytest.raises(ValueError, match='prediction file'):
        evaluate_gold_bundle(**kwargs, split_plan_path=plan_path)
    predictions = []
    for item in load_gold_set(output / 'gold.jsonl'):
        predictions.append({'id': item['id'], 'route': 'research', 'answered': False,
            'retrieved_knowledge_ids': [], 'selected_knowledge_id': None, 'claim_ids': [],
            'abstention_reason': 'no_match', 'resolved_scope': {}})
    _write_private_jsonl(kwargs['predictions_path'], predictions)
    report = evaluate_gold_bundle(**kwargs, split_plan_path=plan_path)
    assert report['acceptance_split']['lineage_valid']
    assert report['acceptance_split']['counts']['acceptance'] == len(candidates)
    assert not report['acceptance_split']['release_eligible']
    assert report['evaluation']['metrics']['sample_size'] == len(candidates)
    plan['candidate_digest'] = '0' * 64
    plan_path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match='candidate batch'):
        evaluate_gold_bundle(**kwargs, split_plan_path=plan_path)


def test_gold_bundle_verifier_detects_post_promotion_tampering(conn, tmp_path):
    candidates_path, candidates, candidate_digest = _two_candidate_batch(conn, tmp_path)
    reviews_path = tmp_path / "reviews.jsonl"
    _write_private_jsonl(
        reviews_path,
        [
            _review_row(candidates[0], candidate_digest),
            _review_row(candidates[1], candidate_digest, decision="rejected"),
        ],
    )
    output = tmp_path / "gold"
    promote_reviewed_candidates(
        candidates_path=candidates_path,
        reviews_path=reviews_path,
        output_dir=output,
    )
    gold_path = output / "gold.jsonl"
    gold_path.write_text(
        gold_path.read_text(encoding="utf-8").replace(
            '"product":"K3"', '"product":"K2"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeGoldReviewError, match="gold digest"):
        verify_gold_bundle(output)


def test_trusted_evaluation_rejects_partial_gold_bundle(conn, tmp_path):
    candidates_path, candidates, candidate_digest = _two_candidate_batch(conn, tmp_path)
    reviews_path = tmp_path / "reviews.jsonl"
    _write_private_jsonl(reviews_path, [_review_row(candidates[0], candidate_digest)])
    output = tmp_path / "partial-gold"
    promoted = promote_reviewed_candidates(
        candidates_path=candidates_path,
        reviews_path=reviews_path,
        output_dir=output,
        allow_partial=True,
    )

    with pytest.raises(KnowledgeGoldReviewError, match="partial gold"):
        evaluate_gold_bundle(
            bundle_dir=output,
            predictions_path=tmp_path / "unused.jsonl",
            approved_manifest_digest=promoted["manifest_digest"],
        )
