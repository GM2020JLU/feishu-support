"""Replay reviewed questions through the live query entry; never accept predictions.

These reports are unsigned review candidates, not release authority. Signing is
performed by an independent owner-controlled system outside this package.
"""

from __future__ import annotations

import copy
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .ids import canonical_json, digest, new_id
from .knowledge_eval import compare_evaluations, evaluate_items
from .knowledge_gold_review import _verify_gold_bundle
from .knowledge_release import (
    MAX_ARTIFACT_BYTES,
    KnowledgeReleaseError,
    current_binding,
    knowledge_snapshot,
)
from .knowledge_runtime import corpus_rows, query_knowledge

_CONTEXT_KEYS = {"requester_id", "chat_id", "observed_scope", "verified_profile"}


def _inputs(gold: list[dict], contexts: dict[str, dict] | None) -> list[dict]:
    supplied = contexts or {}
    if not isinstance(supplied, dict) or set(supplied) - {item["id"] for item in gold}:
        raise KnowledgeReleaseError("request contexts contain unknown question IDs")
    inputs = []
    for case in gold:
        context = supplied.get(case["id"], {})
        if not isinstance(context, dict) or set(context) - _CONTEXT_KEYS:
            raise KnowledgeReleaseError("runtime context must contain observations, never Gold labels")
        for key in ("requester_id", "chat_id"):
            if context.get(key) is not None and not isinstance(context[key], str):
                raise KnowledgeReleaseError("request identity must be a string or null")
        inputs.append({"id": case["id"], "query": case["query"],
                       "requester_id": context.get("requester_id"), "chat_id": context.get("chat_id"),
                       "observed_scope": copy.deepcopy(context.get("observed_scope")),
                       "verified_profile": copy.deepcopy(context.get("verified_profile"))})
    return inputs


def replay_queries(conn, config, *, gold: list[dict], request_inputs: list[dict],
                   selector=None, hybrid=None, options: dict | None = None) -> dict[str, Any]:
    """The only predictor input is the question and separately recorded observations."""
    if [item["id"] for item in request_inputs] != [item["id"] for item in gold]:
        raise KnowledgeReleaseError("runtime inputs and question IDs differ")
    before = current_binding(conn, config)
    snapshot = knowledge_snapshot(conn)
    stable_ids = {row["knowledge_id"]: (
        json.loads(row["revision_payload"])["id"] if row.get("revision_payload")
        else row["knowledge_id"]
    ) for row in corpus_rows(conn)}
    predictions, traces, bindings, observed_bindings = [], [], {}, {}
    started = time.monotonic()
    for item in request_inputs:
        # No expected_route/allowed_ids/required_scope enters this call.
        result = query_knowledge(
            conn, query=item["query"], requester_id=item["requester_id"], chat_id=item["chat_id"],
            observed_scope=item["observed_scope"], verified_profile=item["verified_profile"],
            selector=selector, hybrid=hybrid, options=options or config.raw["knowledge_retrieval"],
            minimum_confidence=config.raw["policy"]["auto_reply_confidence"],
        )
        entry = result.get("selected_entry")
        selected = result["selected_knowledge_id"]
        allowed = selected in snapshot["entries"]
        reason = result["abstention_reason"] or (None if allowed else "insufficient_validation")
        claims = entry.get("knowledge_claim_ids", []) if entry else []
        answered = bool(allowed and entry and claims)
        if not answered and reason is None:
            reason = "insufficient_validation"
        predictions.append({
            "id": item["id"], "route": "direct_answer" if answered else "research", "answered": answered,
            "retrieved_knowledge_ids": [stable_ids[key] for key in result["retrieved_knowledge_ids"]],
            "selected_knowledge_id": stable_ids[selected] if answered else None,
            "claim_ids": claims if answered else [], "abstention_reason": None if answered else reason,
            "resolved_scope": result["scope"],
        })
        observed_bindings[digest(result["runtime_binding"])] = result["runtime_binding"]
        # Release allowlists cover answer-producing runtimes, never abstentions
        # where the selector was not invoked. Keep every observation separately.
        if answered:
            bindings[digest(result["runtime_binding"])] = result["runtime_binding"]
        traces.append({"id": item["id"], **result["trace"]})
    if current_binding(conn, config) != before:
        raise KnowledgeReleaseError("runtime code, corpus or policy changed during evaluation; discard candidate")
    report = evaluate_items(gold, predictions)
    return {"predictions": predictions, "predictions_digest": digest(predictions),
            "report": report, "runtime_bindings": [bindings[key] for key in sorted(bindings)],
            "observed_runtime_bindings": [observed_bindings[key] for key in sorted(observed_bindings)],
            "traces": traces, "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "monetary_cost": None, "cost_note": "No provider billing evidence; not assumed zero."}


def prepare_release_candidate(conn, config, *, gold_bundle: Path, approved_gold_digest: str,
                              instance_id: str, key_id: str, contexts: dict | None = None,
                              selector=None, hybrid=None, validity_hours: int = 24,
                              evidence_class: str = "unreviewed", paired_baseline: bool = False) -> dict:
    verification, gold = _verify_gold_bundle(gold_bundle, expected_manifest_digest=approved_gold_digest)
    if not verification["complete"]:
        raise KnowledgeReleaseError("partial Gold review cannot produce release evidence")
    if type(validity_hours) is not int or not 1 <= validity_hours <= 24 * 31:
        raise KnowledgeReleaseError("candidate validity must be 1..744 hours")
    if evidence_class not in {"unreviewed", "synthetic", "human_reviewed"}:
        raise KnowledgeReleaseError("invalid candidate evidence class")
    if not instance_id.strip() or not key_id.strip():
        raise KnowledgeReleaseError("candidate needs intended instance and independent signer key ID")
    if evidence_class == "human_reviewed" and any("synthetic" in item["tags"] for item in gold):
        raise KnowledgeReleaseError("synthetic Gold cannot be labelled production human review")
    inputs = _inputs(gold, contexts)
    manifest = json.loads((gold_bundle / "manifest.json").read_text(encoding="utf-8"))
    reviews = [json.loads(line) for line in (gold_bundle / "reviews.jsonl").read_text(encoding="utf-8").splitlines() if line]
    from .knowledge_gold_review import verify_embedded_gold_bundle

    embedded = verify_embedded_gold_bundle(manifest, reviews, gold, approved_gold_digest)
    if embedded["review_digest"] != verification["review_digest"]:
        raise KnowledgeReleaseError("review bundle changed during evaluation setup")
    before = current_binding(conn, config)
    run = replay_queries(conn, config, gold=gold, request_inputs=inputs, selector=selector, hybrid=hybrid)
    comparison = None
    if paired_baseline:
        baseline_options = {**config.raw["knowledge_retrieval"], "backend": "sqlite"}
        baseline = replay_queries(conn, config, gold=gold, request_inputs=inputs,
                                  selector=selector, options=baseline_options)
        comparison = {"dataset_size": len(gold), "request_inputs_digest": digest(inputs),
                      "comparison_scope": "retrieval_backend_with_same_selector",
                      "selector_reused": True,
                      "deterministic_provider_output_verified": False,
                      "baseline": baseline, "candidate": run,
                      "comparison": compare_evaluations(baseline["report"], run["report"]),
                      "evidence_class": evidence_class,
                      "quality_claim": "Measured corpus only; small/synthetic data is not production quality proof."}
    if current_binding(conn, config) != before:
        raise KnowledgeReleaseError("runtime changed during paired evaluation")
    now = datetime.now(UTC)
    payload = {"schema_version": 1, "release_id": new_id("krel"), "instance_id": instance_id,
               "key_id": key_id, "issued_at": now.isoformat(),
               "expires_at": (now + timedelta(hours=validity_hours)).isoformat(),
               "evidence_class": evidence_class, "binding": before,
               "entries": knowledge_snapshot(conn)["entries"], "runtime_bindings": run["runtime_bindings"],
               "evaluation": {"origin": "actual_query_runtime", "gold_manifest_digest": verification["manifest_digest"],
                   "gold_digest": verification["gold_digest"], "review_digest": verification["review_digest"],
                   "gold_manifest": manifest, "reviews": reviews,
                   "request_inputs_digest": digest(inputs), "request_inputs": inputs,
                   "gold": gold, "predictions": run["predictions"], "predictions_digest": run["predictions_digest"],
                   "report": run["report"],
                   "runtime_measurements": {key: run[key] for key in (
                       "traces", "elapsed_ms", "monetary_cost", "cost_note", "observed_runtime_bindings")}}}
    return {"status": "unsigned_owner_review_required", "authorized": False, "payload": payload,
            "candidate_digest": digest(payload), "paired_comparison": comparison}


def write_candidate(path: Path, value: dict) -> None:
    """Never overwrite prior review evidence; output includes no private key."""
    if path.is_symlink() or path.exists():
        raise KnowledgeReleaseError("candidate output already exists")
    encoded = (canonical_json(value) + "\n").encode()
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise KnowledgeReleaseError("candidate exceeds the release artifact size limit")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
