from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_professional_knowledge import _approve, _metadata, _write_repo

from k3_support import knowledge_release as release
from k3_support.config import Config, validate_config
from k3_support.ids import canonical_json, digest
from k3_support.knowledge_eval import evaluate_items
from k3_support.knowledge_runtime import query_knowledge
from k3_support.professional_knowledge import (
    compile_repository,
    import_bundle,
    write_bundle,
)


def observed_fixture_selector(query, catalog):
    """Synthetic SDK observation for signed-fixture tests, never a real model."""
    from k3_support.semantic import observed_inference
    result = {'knowledge_id': catalog[0]['knowledge_id'], 'confidence': .99}
    observed_inference.set({'result_digest': digest(result),
        'verification': 'bridge_sdk_observation', 'model': 'fixture-model',
        'provider': 'fixture-provider', 'manifest_digest': 'a'*64})
    return result


@pytest.mark.parametrize('change', [
    {'provider': 'sqlite-lexical', 'verification': 'local_code'},
    {'verification': 'unverified'}, {'protocol': True}, {'protocol': 1},
    {'manifest_digest': 'unknown'}, {'model': ''}, {'provider': None},
    {'extra': 'unrecognized'},
])
def test_release_requires_complete_semantic_observation(change):
    selection = {'provider': 'fixture-provider', 'model': 'fixture-model',
                 'manifest_digest': 'a'*64, 'verification': 'bridge_sdk_observation', 'protocol': 2}
    binding = {'selection': selection, 'effective_backend': 'sqlite', 'index': None}
    assert release._runtime_ready(binding)
    assert not release._runtime_ready(binding | {'selection': selection | change})
    assert not release._runtime_ready(binding | {'effective_backend': 'qdrant'})


def signed_fixture(conn, config, tmp_path, monkeypatch):
    """Synthetic-only trust is injected in tests; no test root is shipped."""
    data = _metadata(automatic_reply=True)
    data["publication"]["answer_visibility"] = "public"
    data["publication"]["source_body_visibility"] = "public"
    data["sources"][0]["visibility"] = "public"
    root = tmp_path / "article-fixture"
    _write_repo(root, _approve(data))
    bundle = compile_repository(root)
    bundle_path = tmp_path / "professional-fixture.json"
    write_bundle(bundle, bundle_path)
    import_bundle(conn, bundle_path=bundle_path, approved_digest=bundle["bundle_digest"], reviewer_id="fixture")
    # Build the synthetic query generation before evaluating and signing it.
    # Imports alone intentionally do not make a runtime generation current.
    from k3_support.knowledge_corpus import build
    assert build(conn)["built"]
    raw = copy.deepcopy(config.raw)
    raw["knowledge_release"] = {"artifact_path": str(tmp_path / "release.json"),
                                "trust_policy_path": str(tmp_path / "test-only-policy")}
    config = Config(validate_config(raw), config.path)
    key = Ed25519PrivateKey.generate()
    policy = {"instance_id": "fixture-instance", "keys": {"fixture-key": base64.b64encode(
        key.public_key().public_bytes_raw()).decode()}, "revoked_release_ids": [],
        "max_validity_seconds": 86400, "accepted_evidence_class": "synthetic"}
    monkeypatch.setattr(release, "load_trust_policy", lambda _: policy)
    query = "K3 U-Boot 里怎么确认 UFS 是否识别？"
    scope = {"software_version": "commit-1", "product": "K3", "component": "u-boot",
             "boot_stage": "u-boot", "storage_medium": "ufs"}
    observed = query_knowledge(conn, query=query, requester_id=None, chat_id=None,
                              observed_scope=scope, options=config.raw["knowledge_retrieval"],
                              selector=observed_fixture_selector)
    assert observed["selected_entry"] is not None
    snapshot = release.knowledge_snapshot(conn)
    stable = next(iter(snapshot["entries"].values()))["stable_id"]
    gold, predictions = [], []
    for index in range(150):
        positive = index < 100
        case_id = f"candidate-{index:024x}"
        gold.append({"id": case_id, "query": query if positive else "unrelated topic",
                     "expected_route": "direct_answer" if positive else "research", "answerable": positive,
                     "allowed_knowledge_ids": [stable] if positive else [],
                     "allowed_claim_ids": ["inspect-command"] if positive else [],
                     "forbidden_knowledge_ids": [] if positive else [stable],
                     "required_scope": scope if positive else {}, "clarification_allowed": False,
                     "acceptable_abstention_reasons": [] if positive else ["no_match"],
                     "tags": ["synthetic", "human_reviewed", "positive" if positive else "hard_negative"]})
        predictions.append({"id": case_id, "route": "direct_answer" if positive else "research",
                            "answered": positive, "retrieved_knowledge_ids": [stable] if positive else [],
                            "selected_knowledge_id": stable if positive else None,
                            "claim_ids": ["inspect-command"] if positive else [],
                            "abstention_reason": None if positive else "no_match", "resolved_scope": scope if positive else {}})
    now = datetime.now(UTC)
    reviews, manifest_cases, inputs = [], [], []
    stamp = "2026-09-01T00:00:00+00:00"
    for item in gold:
        reviews.append({"schema_version": 1, "candidate_id": item["id"], "candidate_digest": "a" * 64,
                        "query_digest": hashlib.sha256(item["query"].encode()).hexdigest(), "decision": "accepted",
                        "reviewed_by": "synthetic-fixture", "reviewed_at": stamp,
                        "notes": "Synthetic test only; not a human production review",
                        "label": {key: value for key, value in item.items() if key not in {"id", "query"}}})
        manifest_cases.append({"id": item["id"], "reviewed_by": "synthetic-fixture", "reviewed_at": stamp,
                               "source_type": "approved_knowledge", "source_id": "fixture-input"})
        inputs.append({"id": item["id"], "query": item["query"], "requester_id": None, "chat_id": None,
                       "observed_scope": scope if item["answerable"] else None, "verified_profile": None})
    def jsonl_hash(items):
        return hashlib.sha256("".join(canonical_json(item) + "\n" for item in items).encode()).hexdigest()
    manifest = {"schema_version": 1, "candidate_digest": "a" * 64, "candidate_count": len(gold),
                "reviewed_count": len(gold), "accepted_count": len(gold), "rejected_count": 0,
                "unreviewed_count": 0, "complete": True, "reviewers": ["synthetic-fixture"],
                "reviewed_at_min": stamp, "reviewed_at_max": stamp, "cases": manifest_cases,
                "gold_digest": jsonl_hash(gold), "review_digest": jsonl_hash(reviews)}
    manifest_digest = hashlib.sha256((canonical_json(manifest) + "\n").encode()).hexdigest()
    payload = {"schema_version": 1, "release_id": "synthetic-fixture-release", "instance_id": policy["instance_id"],
               "key_id": "fixture-key", "issued_at": (now - timedelta(seconds=1)).isoformat(),
               "expires_at": (now + timedelta(hours=1)).isoformat(), "evidence_class": "synthetic",
               "binding": release.current_binding(conn, config), "entries": snapshot["entries"],
               "runtime_bindings": [observed["runtime_binding"]],
               "evaluation": {"origin": "actual_query_runtime", "gold_manifest_digest": manifest_digest,
                   "gold_digest": manifest["gold_digest"], "review_digest": manifest["review_digest"],
                   "gold_manifest": manifest, "reviews": reviews, "request_inputs_digest": digest(inputs), "request_inputs": inputs,
                   "gold": gold, "predictions": predictions, "predictions_digest": digest(predictions),
                   "report": evaluate_items(gold, predictions)}}

    def write(value=payload):
        envelope = {"payload": value, "signature": base64.b64encode(key.sign(canonical_json(value).encode())).decode()}
        (tmp_path / "release.json").write_text(canonical_json(envelope), encoding="utf-8")

    write()
    return config, payload, policy, observed, write


def test_unconfigured_installed_verifier_never_defaults_to_ready(conn, config):
    result = release.verify_release(conn, config)
    assert result == {"ready": False, "reason": "knowledge_release_unconfigured", "release_id": None}


@pytest.mark.parametrize('change', ['old_version', 'missing_dataset', 'different_dataset'])
def test_signed_report_cannot_bypass_current_evaluator_binding(conn, config, tmp_path, monkeypatch, change):
    cfg, payload, _, observed, write = signed_fixture(conn, config, tmp_path, monkeypatch)
    report = payload['evaluation']['report']
    if change == 'old_version':
        report['evaluator_version'] = 2
    elif change == 'missing_dataset':
        report.pop('dataset_digest')
    else:
        report['dataset_digest'] = '0' * 64
    write()  # Even a valid fixture signature cannot replace current re-evaluation.
    result = release.verify_release(conn, cfg, runtime_binding=observed['runtime_binding'])
    assert not result['ready']
    assert result['reason'] == 'release_quality_gate_failed'


def test_valid_isolated_signed_fixture_and_usage_counters_are_not_content(conn, config, tmp_path, monkeypatch):
    cfg, payload, _, observed, _ = signed_fixture(conn, config, tmp_path, monkeypatch)
    result = release.verify_release(conn, cfg, runtime_binding=observed["runtime_binding"],
                                    knowledge_ids=list(payload["entries"]))
    assert result["ready"], result
    conn.execute("UPDATE knowledge_entries SET use_count=use_count+1,updated_at='later'")
    conn.execute("UPDATE source_registry SET last_checked_at='later'")
    assert release.verify_release(conn, cfg, runtime_binding=observed["runtime_binding"])["ready"]
    assert release.verify_release(conn, cfg)["artifact_verified"]
    assert not release.verify_release(conn, cfg)["ready"]


@pytest.mark.parametrize("mutation,reason", [
    ("content", "changed"), ("source", "changed"), ("policy", "changed"),
    ("signature", "signature"), ("revoke", "revoked"), ("expire", "expired"),
    ("wrong_instance", "another instance"), ("synthetic_production", "not_authorized"),
    ("unverified_model", "unverified"), ("ideal_provenance", "actual_runtime"),
    ("prediction", "predictions_changed"), ("failed_gate", "quality_gate"),
])
def test_signed_release_fail_closed(conn, config, tmp_path, monkeypatch, mutation, reason):
    cfg, payload, policy, _, write = signed_fixture(conn, config, tmp_path, monkeypatch)
    if mutation == "content":
        conn.execute("UPDATE knowledge_entries SET answer_markdown='changed content'")
    elif mutation == "source":
        conn.execute("UPDATE source_registry SET content_digest='changed'")
    elif mutation == "policy":
        cfg.raw["routing"]["minimum_route_confidence"] = 0.9
    elif mutation == "signature":
        envelope = json.loads((tmp_path / "release.json").read_text())
        envelope["payload"]["release_id"] = "forged"
        (tmp_path / "release.json").write_text(canonical_json(envelope))
    elif mutation == "revoke":
        policy["revoked_release_ids"].append(payload["release_id"])
    elif mutation == "synthetic_production":
        policy["accepted_evidence_class"] = "human_reviewed"
    else:
        if mutation == "expire":
            payload["expires_at"] = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
        elif mutation == "wrong_instance":
            payload["instance_id"] = "other-instance"
        elif mutation == "unverified_model":
            payload["runtime_bindings"][0]["selection"] = {"provider": "hermes", "verification": "unverified"}
        elif mutation == "ideal_provenance":
            payload["evaluation"]["origin"] = "user_supplied_predictions"
        elif mutation == "prediction":
            payload["evaluation"]["predictions"][0]["claim_ids"] = ["invented"]
        elif mutation == "failed_gate":
            payload["evaluation"]["report"]["ready_for_automatic_reply"] = False
        write()
    result = release.verify_release(conn, cfg)
    assert not result["ready"] and reason in result["reason"], result


def test_production_trust_refuses_agent_owned_file_and_symlink(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text('{}')
    with pytest.raises(release.KnowledgeReleaseError, match="root-owned"):
        release.load_trust_policy(policy)
    link = tmp_path / "policy-link"
    link.symlink_to(policy)
    with pytest.raises(release.KnowledgeReleaseError, match="symlinks"):
        release.load_trust_policy(link)
