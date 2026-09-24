from __future__ import annotations

import copy
import json

import pytest
from test_knowledge_release import signed_fixture, observed_fixture_selector

from k3_support import knowledge_runtime_evaluation as evaluation
from k3_support.ids import canonical_json, digest
from k3_support.knowledge_release import KnowledgeReleaseError, verify_release


def bundle_fixture(tmp_path, payload):
    proof = payload["evaluation"]
    root = tmp_path / "reviewed-fixture-gold"
    root.mkdir(mode=0o700)
    for filename, value in (
        ("manifest.json", canonical_json(proof["gold_manifest"]) + "\n"),
        ("reviews.jsonl", "".join(canonical_json(item) + "\n" for item in proof["reviews"])),
        ("gold.jsonl", "".join(canonical_json(item) + "\n" for item in proof["gold"])),
    ):
        path = root / filename
        path.write_text(value)
        path.chmod(0o600)
    return root


def test_actual_replay_uses_live_query_and_not_gold_scope(conn, config, tmp_path, monkeypatch):
    cfg, payload, _, _, _ = signed_fixture(conn, config, tmp_path, monkeypatch)
    gold = payload["evaluation"]["gold"][:2]
    inputs = evaluation._inputs(gold, None)
    # Gold requires commit-1, but a colleague who did not state a version has
    # not magically supplied it. Real query runtime must abstain.
    replay = evaluation.replay_queries(conn, cfg, gold=gold, request_inputs=inputs)
    assert all(not item["answered"] for item in replay["predictions"])
    assert all("software_version" not in item["resolved_scope"] for item in replay["predictions"])
    assert not replay["report"]["ready_for_automatic_reply"]
    with pytest.raises(KnowledgeReleaseError, match="never Gold labels"):
        evaluation._inputs(gold, {gold[0]["id"]: {"allowed_knowledge_ids": ["desired-answer"]}})


def test_unsigned_candidate_embeds_actual_run_and_paired_same_inputs(conn, config, tmp_path, monkeypatch):
    cfg, payload, _, _, write_signed_fixture = signed_fixture(conn, config, tmp_path, monkeypatch)
    root = bundle_fixture(tmp_path, payload)
    contexts = {item["id"]: {key: value for key, value in item.items() if key not in {"id", "query"}}
                for item in payload["evaluation"]["request_inputs"]}
    candidate = evaluation.prepare_release_candidate(
        conn, cfg, gold_bundle=root, approved_gold_digest=payload["evaluation"]["gold_manifest_digest"],
        instance_id="fixture-instance", key_id="fixture-key", evidence_class="synthetic", contexts=contexts,
        paired_baseline=True,
        selector=observed_fixture_selector,
    )
    assert candidate["authorized"] is False and "signature" not in candidate
    proof = candidate["payload"]["evaluation"]
    assert len(proof["predictions"]) == 150
    assert proof["predictions_digest"] == digest(proof["predictions"])
    assert candidate["paired_comparison"]["dataset_size"] == 150
    assert candidate["paired_comparison"]["request_inputs_digest"] == digest(proof["request_inputs"])
    assert candidate["paired_comparison"]["comparison"]["no_case_regressions"]
    assert proof["runtime_measurements"]["monetary_cost"] is None
    assert len(proof["runtime_measurements"]["traces"]) == 150
    assert candidate["candidate_digest"] == digest(candidate["payload"])
    assert proof["report"]["ready_for_automatic_reply"]  # Synthetic repeated fixtures only.
    assert len(candidate['payload']['runtime_bindings']) == 1
    assert len(proof['runtime_measurements']['observed_runtime_bindings']) == 2
    write_signed_fixture(candidate["payload"])
    verified = verify_release(conn, cfg, runtime_binding=candidate["payload"]["runtime_bindings"][0])
    assert verified["ready"] and verified["evidence_class"] == "synthetic", verified
    output = tmp_path / "unsigned-candidate.json"
    evaluation.write_candidate(output, candidate)
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["authorized"] is False
    with pytest.raises(KnowledgeReleaseError, match="already exists"):
        evaluation.write_candidate(output, candidate)
    with pytest.raises(KnowledgeReleaseError, match="synthetic Gold"):
        evaluation.prepare_release_candidate(
            conn, cfg, gold_bundle=root, approved_gold_digest=proof["gold_manifest_digest"],
            instance_id="fixture", key_id="fixture", evidence_class="human_reviewed",
        )


def test_evaluation_rejects_content_change_during_provider(conn, config, tmp_path, monkeypatch):
    cfg, payload, _, _, _ = signed_fixture(conn, config, tmp_path, monkeypatch)
    gold = copy.deepcopy(payload["evaluation"]["gold"][:1])
    inputs = payload["evaluation"]["request_inputs"][:1]
    def selector(query, catalog):
        conn.execute("UPDATE knowledge_entries SET answer_markdown='changed while model ran'")
        return {"knowledge_id": catalog[0]["knowledge_id"], "confidence": 1.0}
    with pytest.raises(KnowledgeReleaseError, match="changed during evaluation"):
        evaluation.replay_queries(conn, cfg, gold=gold, request_inputs=inputs, selector=selector)


def test_paired_backend_comparison_keeps_same_selector(conn, config, tmp_path, monkeypatch):
    cfg, payload, _, _, _ = signed_fixture(conn, config, tmp_path, monkeypatch)
    root = bundle_fixture(tmp_path, payload)
    contexts = {item['id']: {key: value for key, value in item.items()
                            if key not in {'id', 'query'}}
                for item in payload['evaluation']['request_inputs']}
    calls = []
    real_query = evaluation.query_knowledge

    def selector(query, catalog):
        # Deliberate abstention must apply to BOTH backends. The deterministic
        # SQLite fallback would answer these fixtures if the selector were lost.
        return None

    def observed_query(*args, **kwargs):
        calls.append(kwargs['selector'])
        return real_query(*args, **kwargs)

    monkeypatch.setattr(evaluation, 'query_knowledge', observed_query)
    candidate = evaluation.prepare_release_candidate(
        conn, cfg, gold_bundle=root,
        approved_gold_digest=payload['evaluation']['gold_manifest_digest'],
        instance_id='fixture', key_id='fixture', evidence_class='synthetic',
        contexts=contexts, selector=selector, paired_baseline=True,
    )
    comparison = candidate['paired_comparison']
    assert len(calls) == 2 * len(payload['evaluation']['gold'])
    assert all(value is selector for value in calls)
    for run in (comparison['baseline'], comparison['candidate']):
        assert all(not item['answered'] for item in run['predictions'])
    assert comparison['comparison']['no_case_regressions']
    assert comparison['comparison_scope'] == 'retrieval_backend_with_same_selector'
    assert comparison['selector_reused'] is True
    assert comparison['deterministic_provider_output_verified'] is False
    assert candidate['authorized'] is False
