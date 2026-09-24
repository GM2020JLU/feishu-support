from __future__ import annotations

import pytest

from k3_support.db import connect, migrate
from k3_support.knowledge import (
    attach_registered_source,
    create_candidate,
    register_source,
    review,
)
from k3_support.knowledge_bundle import (
    KnowledgeBundleError,
    export_bundle,
    import_bundle,
    load_bundle,
    plan_import,
)


def _approved_knowledge(conn) -> str:
    register_source(
        conn,
        source_type="git",
        stable_external_id="uboot/uboot:k3-dev",
        title="K3 U-Boot",
        url=None,
        acl={"visibility": "internal"},
        source_version="commit-a",
        content_digest=None,
        updated_at="2026-09-02T10:00:00+08:00",
    )
    knowledge_id = create_candidate(
        conn,
        title="Enter U-Boot",
        questions=["怎么进入 U-Boot？"],
        answer_markdown="复位时持续输入小写 `s`。",
        project="K3",
        module="Boot",
        software_version="commit-a",
        disclosure_class="internal",
        confidence=0.95,
        source_authority=0.95,
        canonical_case_id=None,
        source_digest="enter-uboot-commit-a",
    )
    attach_registered_source(
        conn,
        knowledge_id=knowledge_id,
        source_type="git",
        stable_external_id="uboot/uboot:k3-dev",
        claim="autoboot configuration",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    return knowledge_id


def test_knowledge_bundle_is_digest_gated_and_idempotent(conn, tmp_path):
    _approved_knowledge(conn)
    bundle_path = tmp_path / "bundle.json"
    exported = export_bundle(conn, output_path=bundle_path)
    assert bundle_path.stat().st_mode & 0o777 == 0o600
    assert exported["entry_count"] == 1

    target = connect(tmp_path / "target.db")
    migrate(target)
    plan = plan_import(target, bundle_path=bundle_path)
    assert plan["actions"] == {
        "create": 1,
        "approve_candidate": 0,
        "unchanged": 0,
    }
    with pytest.raises(KnowledgeBundleError, match="approved digest"):
        import_bundle(
            target,
            bundle_path=bundle_path,
            approved_digest="wrong",
            reviewer_id="owner",
        )

    imported = import_bundle(
        target,
        bundle_path=bundle_path,
        approved_digest=exported["bundle_digest"],
        reviewer_id="owner",
    )
    repeated = import_bundle(
        target,
        bundle_path=bundle_path,
        approved_digest=exported["bundle_digest"],
        reviewer_id="owner",
    )
    assert imported["actions"]["create"] == 1
    assert repeated["actions"]["unchanged"] == 1
    assert target.execute(
        "SELECT count(*) FROM knowledge_entries WHERE status='approved'"
    ).fetchone()[0] == 1


def test_knowledge_bundle_rejects_tampering(conn, tmp_path):
    _approved_knowledge(conn)
    bundle_path = tmp_path / "bundle.json"
    export_bundle(conn, output_path=bundle_path)
    raw = bundle_path.read_text(encoding="utf-8")
    bundle_path.write_text(raw.replace("小写 `s`", "大写 `S`"), encoding="utf-8")

    with pytest.raises(KnowledgeBundleError, match="bundle digest mismatch"):
        load_bundle(bundle_path)


def test_knowledge_bundle_reapproves_same_reviewed_stale_entry(conn, tmp_path):
    knowledge_id = _approved_knowledge(conn)
    bundle_path = tmp_path / "bundle.json"
    exported = export_bundle(conn, output_path=bundle_path)
    conn.execute(
        "UPDATE knowledge_entries SET status='stale' WHERE knowledge_id=?",
        (knowledge_id,),
    )

    result = import_bundle(
        conn,
        bundle_path=bundle_path,
        approved_digest=exported["bundle_digest"],
        reviewer_id="owner",
    )

    assert result["actions"]["approve_candidate"] == 1
    assert conn.execute(
        "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
    ).fetchone()[0] == "approved"


def test_knowledge_bundle_refuses_symlink_input(conn, tmp_path):
    _approved_knowledge(conn)
    bundle_path = tmp_path / "bundle.json"
    export_bundle(conn, output_path=bundle_path)
    link = tmp_path / "bundle-link.json"
    link.symlink_to(bundle_path)

    with pytest.raises(KnowledgeBundleError, match="regular file"):
        load_bundle(link)
