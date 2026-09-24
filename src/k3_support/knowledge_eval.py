from __future__ import annotations

import json
from collections import Counter, defaultdict
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from .ids import digest


class KnowledgeEvaluationError(ValueError):
    pass


@lru_cache(maxsize=4)
def _validator(name: str) -> Draft202012Validator:
    path = resources.files("k3_support").joinpath(f"schemas/{name}")
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def _load_jsonl(path: Path, schema_name: str) -> list[dict[str, Any]]:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise KnowledgeEvaluationError(f"JSONL input must be a regular file: {path}")
    validator = _validator(schema_name)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > 1_000_000:
            raise KnowledgeEvaluationError(f"line {line_number} exceeds 1 MiB")
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise KnowledgeEvaluationError(
                f"line {line_number} is invalid JSON"
            ) from exc
        errors = sorted(validator.iter_errors(item), key=lambda error: list(error.path))
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.path) or "item"
            raise KnowledgeEvaluationError(
                f"line {line_number} {location}: {first.message}"
            )
        item_id = str(item["id"])
        if item_id in seen:
            raise KnowledgeEvaluationError(f"duplicate evaluation id: {item_id}")
        seen.add(item_id)
        items.append(item)
    if not items:
        raise KnowledgeEvaluationError(f"JSONL input is empty: {path}")
    return items


def load_gold_set(path: Path) -> list[dict[str, Any]]:
    return validate_gold_cases(_load_jsonl(path, "knowledge-evaluation-case-v1.json"))


def validate_gold_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _validate_items(cases, "knowledge-evaluation-case-v1.json")
    for item in cases:
        if item["answerable"] != (item["expected_route"] == "direct_answer"):
            raise KnowledgeEvaluationError(
                f"direct_answer and answerable must agree: {item['id']}"
            )
        if item["answerable"] and not item["allowed_knowledge_ids"]:
            raise KnowledgeEvaluationError(
                f"answerable case has no allowed knowledge: {item['id']}"
            )
        if item["answerable"] and not item["allowed_claim_ids"]:
            raise KnowledgeEvaluationError(
                f"answerable case has no claims: {item['id']}"
            )
        if set(item["allowed_knowledge_ids"]) & set(item["forbidden_knowledge_ids"]):
            raise KnowledgeEvaluationError(
                f"allowed and forbidden knowledge overlap: {item['id']}"
            )
        if item["expected_route"] == "clarify" and not item["clarification_allowed"]:
            raise KnowledgeEvaluationError(
                f"clarify route forbids clarification: {item['id']}"
            )
        if not item["answerable"] and not item["acceptable_abstention_reasons"]:
            raise KnowledgeEvaluationError(
                f"unanswerable case has no accepted abstention: {item['id']}"
            )
    return cases


def _validate_items(items: list[dict[str, Any]], schema: str) -> None:
    if not isinstance(items, list) or not items:
        raise KnowledgeEvaluationError("evaluation input must be a nonempty list")
    seen = set()
    validator = _validator(schema)
    for index, item in enumerate(items):
        error = next(validator.iter_errors(item), None)
        if error:
            raise KnowledgeEvaluationError(f"{schema} row {index + 1}: {error.message}")
        if item["id"] in seen:
            raise KnowledgeEvaluationError(f"duplicate evaluation id: {item['id']}")
        seen.add(item["id"])


def load_predictions(path: Path) -> list[dict[str, Any]]:
    return _load_jsonl(path, "knowledge-evaluation-prediction-v1.json")


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def evaluate_items(
    gold: list[dict[str, Any]], predictions: list[dict[str, Any]]
) -> dict[str, Any]:
    validate_gold_cases(gold)
    _validate_items(predictions, "knowledge-evaluation-prediction-v1.json")
    gold_by_id = {item["id"]: item for item in gold}
    prediction_by_id = {item["id"]: item for item in predictions}
    missing = sorted(set(gold_by_id) - set(prediction_by_id))
    unexpected = sorted(set(prediction_by_id) - set(gold_by_id))
    if missing or unexpected:
        raise KnowledgeEvaluationError(
            f"prediction IDs differ from gold set; missing={missing}, unexpected={unexpected}"
        )

    route_matrix: dict[str, Counter[str]] = defaultdict(Counter)
    route_correct = 0
    answerable_total = 0
    retrieval_hits = 0
    answered_total = 0
    correct_answers = 0
    unanswerable_total = 0
    abstention_hits = 0
    forbidden_retrievals = 0
    unsupported_claims = 0
    wrong_scope = 0
    invalid_abstentions = 0
    hard_negative_total = 0
    forbidden_clarifications = 0
    missed_handoffs = 0
    invalid_predictions = 0
    failures: list[dict[str, Any]] = []
    slices: dict[str, dict[str, Counter[str]]] = {
        "expected_route": defaultdict(Counter),
        "tag": defaultdict(Counter),
    }

    for item_id, expected in gold_by_id.items():
        actual = prediction_by_id[item_id]
        route_matrix[expected["expected_route"]][actual["route"]] += 1
        if actual["route"] == expected["expected_route"]:
            route_correct += 1
        reasons: list[str] = []
        if actual["route"] != expected["expected_route"]:
            reasons.append("route_mismatch")
        if actual["route"] == "clarify" and not expected["clarification_allowed"]:
            forbidden_clarifications += 1
            reasons.append("forbidden_clarification")
        if (
            expected["expected_route"]
            in {"owner_decision", "urgent_notify", "codex_debug"}
            and actual["route"] != expected["expected_route"]
        ):
            missed_handoffs += 1
            reasons.append("missed_handoff")
        if actual["answered"]:
            coherent = (
                actual["route"] == "direct_answer"
                and actual["abstention_reason"] is None
                and actual["selected_knowledge_id"] in actual["retrieved_knowledge_ids"]
                and bool(actual["claim_ids"])
            )
        else:
            coherent = (
                actual["route"] != "direct_answer"
                and actual["selected_knowledge_id"] is None
                and not actual["claim_ids"]
                and actual["abstention_reason"] is not None
            )
        if not coherent:
            invalid_predictions += 1
            reasons.append("inconsistent_prediction")

        if expected["answerable"]:
            answerable_total += 1
            if set(actual["retrieved_knowledge_ids"][:5]) & set(
                expected["allowed_knowledge_ids"]
            ):
                retrieval_hits += 1
            else:
                reasons.append("retrieval_miss_at_5")
        else:
            unanswerable_total += 1
            hard_negative_total += 1
            if not actual["answered"]:
                if (
                    coherent
                    and actual["abstention_reason"]
                    in expected["acceptable_abstention_reasons"]
                ):
                    abstention_hits += 1
                else:
                    invalid_abstentions += 1
                    reasons.append("invalid_abstention_reason")
            else:
                reasons.append("should_have_abstained")
        if "hard_negative" in expected["tags"] and expected["answerable"]:
            hard_negative_total += 1

        forbidden = set(actual["retrieved_knowledge_ids"]) & set(
            expected["forbidden_knowledge_ids"]
        )
        if forbidden:
            forbidden_retrievals += 1
            reasons.append("forbidden_retrieval")
        unsupported = set(actual["claim_ids"]) - set(expected["allowed_claim_ids"])
        if unsupported:
            unsupported_claims += 1
            reasons.append("unsupported_claim")
        scope_ok = True
        for key, value in expected["required_scope"].items():
            if actual["resolved_scope"].get(key) != value:
                scope_ok = False
                wrong_scope += 1
                reasons.append(f"wrong_scope:{key}")
                break
        correct_answer = False
        if actual["answered"]:
            answered_total += 1
            correct_answer = (
                coherent
                and expected["answerable"]
                and actual["selected_knowledge_id"] in expected["allowed_knowledge_ids"]
                and not unsupported
                and not forbidden
                and scope_ok
            )
            if correct_answer:
                correct_answers += 1
            else:
                reasons.append("incorrect_direct_answer")
        for dimension, keys in (
            ("expected_route", [expected["expected_route"]]),
            ("tag", expected["tags"]),
        ):
            for key in keys:
                bucket = slices[dimension][key]
                bucket["case_count"] += 1
                bucket["failure_count"] += bool(reasons)
                bucket["answered_count"] += actual["answered"]
                bucket["correct_answer_count"] += correct_answer
        if reasons:
            failures.append({"id": item_id, "reasons": sorted(set(reasons))})

    sample_size = len(gold)
    hard_negative_ratio = _ratio(hard_negative_total, sample_size)
    metrics = {
        "sample_size": sample_size,
        "hard_negative_ratio": hard_negative_ratio,
        "route_accuracy": _ratio(route_correct, sample_size),
        "answerable_recall_at_5": _ratio(retrieval_hits, answerable_total),
        "direct_answer_precision": _ratio(correct_answers, answered_total),
        "abstention_recall": _ratio(abstention_hits, unanswerable_total),
        "forbidden_retrieval_count": forbidden_retrievals,
        "unsupported_claim_count": unsupported_claims,
        "wrong_scope_count": wrong_scope,
        "invalid_abstention_count": invalid_abstentions,
        "forbidden_clarification_count": forbidden_clarifications,
        "missed_handoff_count": missed_handoffs,
        "invalid_prediction_count": invalid_predictions,
    }
    gates = {
        "minimum_sample_size": sample_size >= 150,
        "hard_negative_coverage": hard_negative_ratio is not None
        and hard_negative_ratio >= 0.30,
        "direct_answer_precision": metrics["direct_answer_precision"] is not None
        and metrics["direct_answer_precision"] >= 0.99,
        "answerable_recall_at_5": metrics["answerable_recall_at_5"] is not None
        and metrics["answerable_recall_at_5"] >= 0.95,
        "abstention_recall": metrics["abstention_recall"] is not None
        and metrics["abstention_recall"] >= 0.98,
        "zero_forbidden_retrievals": forbidden_retrievals == 0,
        "zero_unsupported_claims": unsupported_claims == 0,
        "zero_wrong_scope": wrong_scope == 0,
        "valid_abstention_reasons": invalid_abstentions == 0,
        "zero_forbidden_clarifications": forbidden_clarifications == 0,
        "zero_missed_handoffs": missed_handoffs == 0,
        "consistent_predictions": invalid_predictions == 0,
    }
    return {
        "evaluator_version": 3,
        "dataset_digest": digest(sorted(gold, key=lambda item: item['id'])),
        "metrics": metrics,
        "gates": gates,
        "ready_for_automatic_reply": all(gates.values()),
        "route_confusion_matrix": {
            expected: dict(sorted(actual.items()))
            for expected, actual in sorted(route_matrix.items())
        },
        "failures": failures,
        "slices": {
            dimension: {key: dict(counts) for key, counts in sorted(buckets.items())}
            for dimension, buckets in slices.items()
        },
    }


def evaluate(gold_path: Path, predictions_path: Path) -> dict[str, Any]:
    return evaluate_items(load_gold_set(gold_path), load_predictions(predictions_path))


def compare_evaluations(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Compare reports from the same gold set, retaining per-case regressions."""
    if (baseline.get('evaluator_version') != 3 or candidate.get('evaluator_version') != 3
            or not isinstance(baseline.get('dataset_digest'), str)
            or len(baseline['dataset_digest']) != 64
            or baseline['dataset_digest'] != candidate.get('dataset_digest')):
        raise KnowledgeEvaluationError('comparison requires matching dataset and evaluator bindings; regenerate reports')
    before = {row["id"]: set(row["reasons"]) for row in baseline["failures"]}
    after = {row["id"]: set(row["reasons"]) for row in candidate["failures"]}
    regressions = []
    improvements = []
    for case_id in sorted(set(before) | set(after)):
        added = after.get(case_id, set()) - before.get(case_id, set())
        removed = before.get(case_id, set()) - after.get(case_id, set())
        if added:
            regressions.append({"id": case_id, "reasons": sorted(added)})
        if removed:
            improvements.append({"id": case_id, "reasons": sorted(removed)})
    return {
        "regressions": regressions,
        "improvements": improvements,
        "no_case_regressions": not regressions,
        "metric_deltas": {
            key: candidate["metrics"][key] - value
            for key, value in baseline["metrics"].items()
            if value is not None and candidate["metrics"][key] is not None
        },
        "passed_to_failed_gates": sorted(
            key
            for key, passed in baseline["gates"].items()
            if passed and not candidate["gates"][key]
        ),
        "failed_to_passed_gates": sorted(
            key
            for key, passed in candidate["gates"].items()
            if passed and not baseline["gates"][key]
        ),
    }
