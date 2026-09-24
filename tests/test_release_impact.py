from __future__ import annotations

import json

import pytest

from k3_support.config import Config
from k3_support.knowledge import create_candidate, review
from k3_support.release_impact import ReleaseImpactError, assess_release_change
from k3_support.runtime_control import ensure_global_state, outbox_eligible


def _config(config: Config) -> Config:
    config.raw["mode"] = "active"
    config.raw["repositories"] = {
        "u-boot": {
            "path": "/data/home2/operator/WorkSpace/k3/u-boot",
            "remote": "origin",
            "base_branch": "main",
        }
    }
    return config


def _change() -> dict:
    return {
        "repository": "u-boot",
        "change_id": "I1234567890",
        "revision": "abcdef1234567890",
        "subject": "ufs: update boot lun selection",
        "branch": "main",
        "changed_paths": ["drivers/ufs/ufs.c", "cmd/boot.c"],
    }


def _approved_knowledge(conn) -> str:
    knowledge_id = create_candidate(
        conn,
        title="UFS 启动介质选择",
        questions=["怎么从 UFS 启动"],
        answer_markdown="使用已审核流程。",
        project="K3",
        module="ufs",
        software_version=None,
        disclosure_class="internal",
        confidence=0.95,
        source_authority=0.95,
        canonical_case_id=None,
        source_digest="source-ufs",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    return knowledge_id


def _professional_source(conn, knowledge_id: str) -> None:
    conn.execute(
        """INSERT INTO professional_knowledge_revisions(
               revision_id,stable_id,revision_number,knowledge_id,kind,lifecycle_state,
               revision_digest,payload_json,body_markdown,owner,reviewed_by,reviewed_at,
               review_due_at,imported_at)
           VALUES('kvr_source','k3.uboot.ufs',1,?,'command_reference','published',
                  ?, '{}','answer','owner','owner',?,?,?)""",
        (knowledge_id, "a" * 64, "2026-09-01T00:00:00+00:00",
         "2027-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
    )
    conn.execute(
        "UPDATE knowledge_entries SET professional_revision_id='kvr_source' WHERE knowledge_id=?",
        (knowledge_id,),
    )
    conn.execute(
        """INSERT INTO professional_knowledge_claims(
               claim_id,revision_id,local_claim_id,statement,risk_class,required_validation_json)
           VALUES('kcl_source','kvr_source','ufs-enumeration','claim','read_only','[\"static\"]')"""
    )
    conn.execute(
        """INSERT INTO professional_claim_sources(
               claim_id,source_id,source_type,stable_external_id,source_version,
               snapshot_digest,locator_json)
           VALUES('kcl_source','git-source','git','u-boot:drivers/ufs/ufs.c','abcdef1',?,?)""",
        ("b" * 64, json.dumps({"repository": "u-boot", "path": "drivers/ufs/ufs.c"})),
    )


def test_release_impact_notifies_operator_once_with_exact_knowledge_ids(conn, config):
    cfg = _config(config)
    knowledge_id = _approved_knowledge(conn)
    seen = []

    def analyzer(value):
        seen.append(value)
        return {
            "summary": "可能影响 UFS 启动介质选择和现有答复",
            "impact_level": "high",
            "affected_knowledge_ids": [knowledge_id],
            "risks": ["旧命令行为可能变化"],
            "likely_questions": ["升级后为什么无法从 UFS 启动"],
            "recommended_validation": ["覆盖 UFS 启动与回退路径"],
            "confidence": 0.96,
        }

    first = assess_release_change(conn, cfg, change=_change(), analyzer=analyzer)
    replay = assess_release_change(
        conn,
        cfg,
        change=_change(),
        analyzer=lambda _: (_ for _ in ()).throw(AssertionError("must not rerun")),
    )

    assert first["created"] is True
    assert replay["created"] is False
    assert replay["impact_id"] == first["impact_id"]
    assert len(seen) == 1
    assert seen[0]["change"]["revision"] == "abcdef1234567890"
    row = conn.execute("SELECT action_type,payload_json FROM outbox").fetchone()
    assert row["action_type"] == "release_impact"
    payload = json.loads(row["payload_json"])
    assert knowledge_id in payload["text"]
    assert "不会自动通知同事" in payload["text"]
    ensure_global_state(conn)
    outbox = dict(conn.execute("SELECT * FROM outbox").fetchone())
    assert outbox_eligible(conn, cfg, outbox) is True


def test_release_impact_rejects_invented_knowledge_without_writes(conn, config):
    cfg = _config(config)
    with pytest.raises(ReleaseImpactError, match="invented"):
        assess_release_change(
            conn,
            cfg,
            change=_change(),
            analyzer=lambda _: {
                "summary": "影响未知",
                "impact_level": "medium",
                "affected_knowledge_ids": ["knw_invented"],
                "risks": [],
                "likely_questions": [],
                "recommended_validation": [],
                "confidence": 0.8,
            },
        )
    assert conn.execute("SELECT count(*) FROM release_impacts").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_release_impact_requires_configured_repository(conn, config):
    with pytest.raises(ReleaseImpactError, match="repository"):
        assess_release_change(
            conn,
            config,
            change=_change(),
            analyzer=lambda _: None,
        )


def test_exact_source_path_invalidates_professional_claim_once(conn, config):
    cfg = _config(config)
    knowledge_id = _approved_knowledge(conn)
    _professional_source(conn, knowledge_id)

    assessment = {
        "summary": "UFS source changed",
        "impact_level": "high",
        "affected_knowledge_ids": [knowledge_id],
        "risks": [],
        "likely_questions": [],
        "recommended_validation": ["review source"],
        "confidence": 1.0,
    }
    first = assess_release_change(conn, cfg, change=_change(), analyzer=lambda _: assessment)
    replay = assess_release_change(
        conn,
        cfg,
        change=_change(),
        analyzer=lambda _: (_ for _ in ()).throw(AssertionError("must not rerun")),
    )

    assert len(first["invalidated_claims"]) == 1
    assert replay["invalidated_claims"][0]["local_claim_id"] == "ufs-enumeration"
    assert conn.execute(
        "SELECT lifecycle_state FROM professional_knowledge_revisions"
    ).fetchone()[0] == "needs_review"
    assert conn.execute(
        "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()[0] == "stale"
    assert conn.execute(
        "SELECT count(*) FROM professional_source_change_events"
    ).fetchone()[0] == 1
    payload = json.loads(conn.execute("SELECT payload_json FROM outbox").fetchone()[0])
    assert "已自动下线" in payload["text"]

    later_change = _change()
    later_change["change_id"] = "I2222222222"
    later_change["revision"] = "bbbbbb1234567890"
    later = assess_release_change(
        conn, cfg, change=later_change, analyzer=lambda _: assessment
    )
    assert len(later["invalidated_claims"]) == 1
    assert conn.execute(
        "SELECT count(*) FROM professional_source_change_events"
    ).fetchone()[0] == 2


def test_unrelated_source_path_does_not_invalidate(conn, config):
    cfg = _config(config)
    knowledge_id = _approved_knowledge(conn)
    _professional_source(conn, knowledge_id)
    change = _change()
    change["changed_paths"] = ["drivers/mmc/mmc.c"]
    assessment = {
        "summary": "unrelated",
        "impact_level": "low",
        "affected_knowledge_ids": [],
        "risks": [],
        "likely_questions": [],
        "recommended_validation": [],
        "confidence": 1.0,
    }
    result = assess_release_change(conn, cfg, change=change, analyzer=lambda _: assessment)
    assert result["invalidated_claims"] == []
    assert conn.execute(
        "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()[0] == "approved"


def test_analyzer_failure_cannot_leave_exact_source_match_eligible(conn, config):
    cfg = _config(config)
    knowledge_id = _approved_knowledge(conn)
    _professional_source(conn, knowledge_id)

    with pytest.raises(ReleaseImpactError, match="unavailable"):
        assess_release_change(conn, cfg, change=_change(), analyzer=None)

    assert conn.execute(
        "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()[0] == "stale"
    assert conn.execute(
        "SELECT count(*) FROM professional_source_change_events"
    ).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM release_impacts").fetchone()[0] == 0
