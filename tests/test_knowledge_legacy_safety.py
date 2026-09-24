from __future__ import annotations

import pytest
from test_professional_knowledge import NOW, _approve, _metadata, _write_repo

from k3_support.config import Config
from k3_support.decision import DecisionError, _forbidden_reply_paths, validate_decision
from k3_support.knowledge import KnowledgeError, record_feedback, review
from k3_support.professional_knowledge import (
    compile_repository,
    import_bundle,
    write_bundle,
)


@pytest.mark.parametrize("decision", ["candidate", "retired"])
def test_legacy_review_cannot_republish_professional_revision(conn, tmp_path, decision):
    root = tmp_path / "knowledge"
    _write_repo(root, _approve(_metadata()))
    bundle = compile_repository(root, now=NOW)
    path = tmp_path / "bundle.json"
    write_bundle(bundle, path)
    imported = import_bundle(conn, bundle_path=path, approved_digest=bundle["bundle_digest"], reviewer_id="owner", now=NOW)
    knowledge_id = imported["knowledge_ids"][0]
    review(conn, knowledge_id=knowledge_id, reviewer_id="operator", decision=decision)
    with pytest.raises(KnowledgeError, match="legacy status approval"):
        review(conn, knowledge_id=knowledge_id, reviewer_id="operator", decision="approved")
    assert conn.execute("SELECT status FROM knowledge_entries").fetchone()[0] == decision
    expected = "retired" if decision == "retired" else "needs_review"
    assert conn.execute("SELECT lifecycle_state FROM professional_knowledge_revisions").fetchone()[0] == expected
    # Replaying the exact old approved bundle is idempotent, not republication.
    import_bundle(conn, bundle_path=path, approved_digest=bundle["bundle_digest"], reviewer_id="owner", now=NOW)
    assert conn.execute("SELECT status FROM knowledge_entries").fetchone()[0] == decision


def test_negative_feedback_stales_both_professional_and_legacy_projection(conn, tmp_path):
    root = tmp_path / "knowledge"
    _write_repo(root, _approve(_metadata()))
    bundle = compile_repository(root, now=NOW)
    path = tmp_path / "bundle.json"
    write_bundle(bundle, path)
    knowledge_id = import_bundle(conn, bundle_path=path, approved_digest=bundle["bundle_digest"], reviewer_id="owner", now=NOW)["knowledge_ids"][0]
    record_feedback(conn, knowledge_id=knowledge_id, actor_id="operator", verdict="helpful")
    assert conn.execute("SELECT success_count FROM knowledge_entries").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM knowledge_feedback WHERE verdict='helpful'").fetchone()[0] == 1
    record_feedback(conn, knowledge_id=knowledge_id, actor_id="operator", verdict="incorrect")
    assert conn.execute("SELECT status FROM knowledge_entries").fetchone()[0] == "stale"
    assert conn.execute("SELECT lifecycle_state FROM professional_knowledge_revisions").fetchone()[0] == "needs_review"


def test_ai_decision_cannot_assert_operator_resolution():
    decision = {"decision_id": "resolve-ai", "case_id": "K3-20260907-0001", "expected_case_version": 1,
                "intent": "resolve", "confidence": 0.99, "evidence_ids": [], "reply_draft": None,
                "proposed_actions": [], "facts": ["board passed"], "inferences": [], "unknowns": []}
    with pytest.raises(DecisionError, match="operator confirmation"):
        validate_decision(decision)


def test_chat_only_config_does_not_require_disabled_remote_paths(config):
    raw = {**config.raw, "runtime": {key: None for key in config.raw["runtime"]}}
    cfg = Config(raw, config.path)
    assert str(config.data_dir.resolve()) in _forbidden_reply_paths(cfg)
