from __future__ import annotations

import json

import pytest

from k3_support.knowledge_eval import (
    KnowledgeEvaluationError,
    compare_evaluations,
    evaluate,
    evaluate_items,
)


def _gold(case_id: str, *, answerable: bool) -> dict:
    return {
        "id": case_id,
        "query": "U-Boot 怎么查看 UFS？" if answerable else "替我承诺明天修好",
        "expected_route": "direct_answer" if answerable else "owner_decision",
        "answerable": answerable,
        "allowed_knowledge_ids": ["k3.uboot.storage.inspect"] if answerable else [],
        "allowed_claim_ids": ["inspect-command"] if answerable else [],
        "forbidden_knowledge_ids": ["private.debug.note"],
        "required_scope": {"product": "K3", "component": "u-boot"},
        "clarification_allowed": False,
        "acceptable_abstention_reasons": [] if answerable else ["high_risk"],
        "tags": ["answerable"] if answerable else ["hard_negative"],
    }


def _prediction(case_id: str, *, answerable: bool) -> dict:
    return {
        "id": case_id,
        "route": "direct_answer" if answerable else "owner_decision",
        "answered": answerable,
        "retrieved_knowledge_ids": ["k3.uboot.storage.inspect"] if answerable else [],
        "selected_knowledge_id": "k3.uboot.storage.inspect" if answerable else None,
        "claim_ids": ["inspect-command"] if answerable else [],
        "abstention_reason": None if answerable else "high_risk",
        "resolved_scope": {"product": "K3", "component": "u-boot"},
    }


def _write_jsonl(path, items):
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
        encoding="utf-8",
    )


def test_missing_semantic_selection_is_valid_abstention_evidence():
    gold = _gold('semantic-missing', answerable=False)
    prediction = _prediction('semantic-missing', answerable=False)
    gold['expected_route'] = prediction['route'] = 'research'
    gold['acceptable_abstention_reasons'] = ['semantic_selection_required']
    prediction['abstention_reason'] = 'semantic_selection_required'
    result = evaluate_items([gold], [prediction])
    assert result['metrics']['abstention_recall'] == 1.0
    assert result['ready_for_automatic_reply'] is False


def test_evaluation_reports_metrics_but_requires_a_real_sample(tmp_path):
    gold_path = tmp_path / "gold.jsonl"
    prediction_path = tmp_path / "prediction.jsonl"
    _write_jsonl(
        gold_path,
        [_gold("answerable-1", answerable=True), _gold("negative-1", answerable=False)],
    )
    _write_jsonl(
        prediction_path,
        [
            _prediction("answerable-1", answerable=True),
            _prediction("negative-1", answerable=False),
        ],
    )

    result = evaluate(gold_path, prediction_path)
    assert result["metrics"]["direct_answer_precision"] == 1.0
    assert result["metrics"]["answerable_recall_at_5"] == 1.0
    assert result["metrics"]["abstention_recall"] == 1.0
    assert result["gates"]["minimum_sample_size"] is False
    assert result["ready_for_automatic_reply"] is False


def test_evaluation_detects_forbidden_retrieval_claim_and_scope(tmp_path):
    gold_path = tmp_path / "gold.jsonl"
    prediction_path = tmp_path / "prediction.jsonl"
    expected = _gold("case-1", answerable=True)
    actual = _prediction("case-1", answerable=True)
    actual["retrieved_knowledge_ids"].insert(0, "private.debug.note")
    actual["claim_ids"] = ["invented-claim"]
    actual["resolved_scope"]["component"] = "edk2"
    _write_jsonl(gold_path, [expected])
    _write_jsonl(prediction_path, [actual])

    result = evaluate(gold_path, prediction_path)
    assert result["metrics"]["forbidden_retrieval_count"] == 1
    assert result["metrics"]["unsupported_claim_count"] == 1
    assert result["metrics"]["wrong_scope_count"] == 1
    assert set(result["failures"][0]["reasons"]) >= {
        "forbidden_retrieval",
        "unsupported_claim",
        "wrong_scope:component",
    }


def test_evaluation_rejects_prediction_set_mismatch(tmp_path):
    gold_path = tmp_path / "gold.jsonl"
    prediction_path = tmp_path / "prediction.jsonl"
    _write_jsonl(gold_path, [_gold("case-1", answerable=True)])
    _write_jsonl(prediction_path, [_prediction("other-case", answerable=True)])

    with pytest.raises(KnowledgeEvaluationError, match="IDs differ"):
        evaluate(gold_path, prediction_path)


def _release_sample():
    gold = [_gold(f"case-{index}", answerable=index < 100) for index in range(150)]
    predictions = [
        _prediction(item["id"], answerable=item["answerable"]) for item in gold
    ]
    return gold, predictions


def test_release_gate_rejects_unwanted_clarification_and_missed_handoff():
    gold, predictions = _release_sample()
    predictions[-1]["route"] = "clarify"

    result = evaluate_items(gold, predictions)

    assert result["ready_for_automatic_reply"] is False
    assert result["metrics"]["forbidden_clarification_count"] == 1
    assert result["metrics"]["missed_handoff_count"] == 1
    assert set(result["failures"][-1]["reasons"]) >= {
        "forbidden_clarification",
        "missed_handoff",
        "route_mismatch",
    }


def test_wrong_scope_answer_is_not_counted_as_correct():
    gold, predictions = _release_sample()
    predictions[0]["resolved_scope"]["product"] = "K1"

    result = evaluate_items(gold, predictions)

    assert result["metrics"]["direct_answer_precision"] == 0.99
    assert "incorrect_direct_answer" in result["failures"][0]["reasons"]


@pytest.mark.parametrize(
    "mutation", ["contradictory_route", "missing_retrieval", "abstention"]
)
def test_answer_requires_a_consistent_prediction(mutation):
    gold, predictions = _release_sample()
    actual = predictions[0]
    if mutation == "contradictory_route":
        actual["route"] = "ignore"
    elif mutation == "missing_retrieval":
        actual["retrieved_knowledge_ids"] = []
    else:
        actual["abstention_reason"] = "high_risk"

    result = evaluate_items(gold, predictions)

    assert result["ready_for_automatic_reply"] is False
    assert result["metrics"]["invalid_prediction_count"] == 1


def test_in_memory_evaluation_rejects_duplicate_ids():
    gold, predictions = _release_sample()
    predictions.append(predictions[0])
    with pytest.raises(KnowledgeEvaluationError, match="duplicate"):
        evaluate_items(gold, predictions)


def test_consistent_release_sample_keeps_existing_precision_gates():
    gold, predictions = _release_sample()
    result = evaluate_items(gold, predictions)
    assert result["ready_for_automatic_reply"] is True


def test_comparison_exposes_a_regression_hidden_by_unchanged_average():
    gold, predictions = _release_sample()
    gold[0]["tags"].append("fan")
    gold[1]["tags"].append("ec")
    predictions[0]["resolved_scope"]["component"] = "edk2"
    baseline = evaluate_items(gold, predictions)
    predictions[0]["resolved_scope"]["component"] = "u-boot"
    predictions[1]["resolved_scope"]["component"] = "edk2"
    candidate = evaluate_items(gold, predictions)
    comparison = compare_evaluations(baseline, candidate)

    assert comparison["metric_deltas"]["direct_answer_precision"] == 0
    assert comparison["improvements"][0]["id"] == "case-0"
    assert comparison["regressions"][0]["id"] == "case-1"
    assert comparison["no_case_regressions"] is False
    assert candidate["slices"]["tag"]["ec"]["failure_count"] == 1
    assert candidate["slices"]["tag"]["fan"]["failure_count"] == 0


@pytest.mark.parametrize('change', ['question', 'expected', 'version', 'missing'])
def test_comparison_rejects_different_or_unbound_evaluation_inputs(change):
    import copy
    gold, predictions = _release_sample()
    baseline = evaluate_items(gold, predictions)
    changed = copy.deepcopy(gold)
    if change == 'question':
        changed[0]['query'] += ' changed'
    elif change == 'expected':
        changed[0]['allowed_claim_ids'] = ['different-verified-claim']
    candidate = evaluate_items(changed, predictions)
    if change == 'version':
        candidate['evaluator_version'] = 2
    elif change == 'missing':
        candidate.pop('dataset_digest')
    with pytest.raises(KnowledgeEvaluationError, match='matching dataset'):
        compare_evaluations(baseline, candidate)


def test_comparison_dataset_binding_is_independent_of_input_order():
    gold, predictions = _release_sample()
    baseline = evaluate_items(gold, predictions)
    candidate = evaluate_items(list(reversed(gold)), list(reversed(predictions)))
    assert compare_evaluations(baseline, candidate)['no_case_regressions']


@pytest.mark.parametrize("invalid", ["route", "claims", "overlap", "clarify"])
def test_gold_semantic_contract_is_validated_for_every_evaluation_entrypoint(invalid):
    item = _gold("case-1", answerable=True)
    if invalid == "route":
        item["expected_route"] = "research"
    elif invalid == "claims":
        item["allowed_claim_ids"] = []
    elif invalid == "overlap":
        item["forbidden_knowledge_ids"] = item["allowed_knowledge_ids"]
    else:
        item = _gold("case-1", answerable=False)
        item["expected_route"] = "clarify"
    with pytest.raises(KnowledgeEvaluationError):
        evaluate_items([item], [_prediction("case-1", answerable=True)])
