from __future__ import annotations

import copy
import hashlib
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from k3_support.knowledge import register_source
from k3_support.orchestrator import _approved_answer_markdown
from k3_support.professional_knowledge import (
    ProfessionalKnowledgeError,
    compile_repository,
    export_legacy_drafts,
    import_bundle,
    legacy_inventory,
    lint_repository,
    load_bundle,
    plan_import,
    revision_digest,
    validate_article,
    verify_git_sources,
    write_bundle,
    write_legacy_inventory,
)
from k3_support.workbench import (
    professional_knowledge_snapshot,
    render_professional_knowledge,
)

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
BODY = "使用只读命令确认设备枚举状态。\n\n不要在未确认版本时执行写入操作。"


def test_import_reads_bundle_once_and_rolls_back_sources_on_failure(conn, tmp_path, monkeypatch):
    import k3_support.professional_knowledge as professional

    root = tmp_path / "knowledge"
    _write_repo(root, _approve(_metadata()))
    bundle = compile_repository(root, now=NOW)
    path = tmp_path / "bundle.json"
    write_bundle(bundle, path)
    original = professional.load_bundle
    reads = []

    def once(*args, **kwargs):
        reads.append(True)
        assert len(reads) == 1
        return original(*args, **kwargs)

    monkeypatch.setattr(professional, "load_bundle", once)
    conn.execute("CREATE TEMP TRIGGER fail_publication BEFORE INSERT ON professional_knowledge_revisions BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    before = list(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        import_bundle(conn, bundle_path=path, approved_digest=bundle["bundle_digest"], reviewer_id="owner", now=NOW)
    assert len(reads) == 1
    assert list(conn.iterdump()) == before
    assert conn.execute("SELECT count(*) FROM source_registry").fetchone()[0] == 0
    assert not conn.in_transaction


def _metadata(*, revision: int = 1, automatic_reply: bool = False) -> dict:
    return {
        "schema_version": 2,
        "id": "k3.uboot.storage.inspect",
        "revision": revision,
        "kind": "command_reference",
        "status": "published",
        "title": "检查 U-Boot 存储设备",
        "owner": "bootloader-team",
        "scope": {
            "product": "K3",
            "component": "u-boot",
            "subcomponent": "storage",
            "basis": "software_only",
            "boards": [],
            "hardware_revisions": [],
            "software_versions": [f"commit-{revision}"],
            "boot_stages": ["u-boot"],
            "storage_media": ["ufs"],
            "operating_systems": [],
        },
        "intent": {
            "aliases": ["查看 UFS"],
            "question_examples": ["U-Boot 里怎么确认 UFS 是否识别？"],
            "required_entities": ["storage_medium"],
            "negative_constraints": ["不要用于刷写"],
        },
        "content": {
            "summary": "使用只读命令检查 UFS 枚举。",
            "prerequisites": ["已经进入 U-Boot 命令行"],
            "steps": ["执行 `ufs info`"],
            "expected_observations": ["输出已枚举的 UFS 设备"],
            "failure_branches": ["无设备时转入 UFS 枚举诊断树"],
            "rollback": [],
            "warnings": ["不要执行写入命令"],
            "commands": ["ufs info"],
        },
        "claims": [
            {
                "id": "inspect-command",
                "statement": "`ufs info` 用于查看 UFS 枚举信息。",
                "source_refs": ["uboot-source"],
                "risk_class": "read_only",
                "required_validation": ["static"],
            }
        ],
        "sources": [
            {
                "id": "uboot-source",
                "type": "git",
                "stable_external_id": "uboot/uboot:k3-dev:cmd/ufs.c",
                "title": "K3 U-Boot UFS command",
                "url": None,
                "version": f"commit-{revision}",
                "snapshot_digest": "a" * 64,
                "authority": 0.95,
                "visibility": "internal",
                "share_mode": "full_answer",
                "locator": {
                    "repository": "uboot/uboot",
                    "commit": f"commit-{revision}",
                    "path": "cmd/ufs.c",
                    "symbol": "do_ufs",
                },
            }
        ],
        "validation": [
            {
                "id": "static-review",
                "claim_refs": ["inspect-command"],
                "layer": "static",
                "result": "passed",
                "environment": {
                    "repository": "uboot/uboot",
                    "commit": f"commit-{revision}",
                },
                "artifact_digest": None,
                "case_id": None,
                "observed_at": "2026-09-03T10:00:00+08:00",
            }
        ],
        "publication": {
            "answer_visibility": "internal",
            "source_body_visibility": "internal",
            "link_policy": "direct",
            "automatic_reply": automatic_reply,
            "allowed_chat_ids": [],
            "allowed_user_ids": [],
        },
        "quality": (
            {
                "evaluation_set_id": "gold-k3-v1",
                "sample_size": 200,
                "direct_answer_precision": 0.99,
                "answerable_recall_at_5": 0.95,
                "abstention_recall": 0.98,
                "evaluated_at": "2026-09-03T11:00:00+08:00",
            }
            if automatic_reply
            else None
        ),
        "review": {
            "reviewed_by": "owner",
            "reviewed_at": "2026-09-03T12:00:00+08:00",
            "review_due_at": "2026-12-03T12:00:00+08:00",
            "approved_revision_digest": "0" * 64,
        },
    }


def _approve(metadata: dict, body: str = BODY) -> dict:
    metadata = copy.deepcopy(metadata)
    metadata["review"]["approved_revision_digest"] = revision_digest(metadata, body)
    return metadata


def _write_repo(root: Path, metadata: dict, body: str = BODY) -> Path:
    (root / "articles" / "uboot").mkdir(parents=True, exist_ok=True)
    (root / "vocabulary.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "products": ["K3"],
                "components": ["u-boot"],
                "boards": ["k3-pico-itx"],
                "boot_stages": ["u-boot"],
                "storage_media": ["ufs"],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    article = root / "articles" / "uboot" / "storage-inspect-r1.md"
    article.write_text(
        "---\n"
        + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + body
        + "\n",
        encoding="utf-8",
    )
    return article


def test_revision_digest_binds_scope_acl_and_body():
    metadata = _metadata()
    original = revision_digest(metadata, BODY)
    changed_scope = copy.deepcopy(metadata)
    changed_scope["scope"]["software_versions"] = ["other-commit"]
    changed_acl = copy.deepcopy(metadata)
    changed_acl["publication"]["answer_visibility"] = "private"

    assert revision_digest(changed_scope, BODY) != original
    assert revision_digest(changed_acl, BODY) != original
    assert revision_digest(metadata, BODY + "\n新增结论") != original


def test_published_article_requires_claim_validation_and_exact_review_digest():
    metadata = _metadata()
    with pytest.raises(ProfessionalKnowledgeError, match="approved_revision_digest"):
        validate_article(metadata, BODY, now=NOW)

    approved = _approve(metadata)
    approved["validation"] = []
    approved = _approve(approved)
    with pytest.raises(ProfessionalKnowledgeError, match="lacks passed validation"):
        validate_article(approved, BODY, now=NOW)


def test_automatic_reply_requires_calibrated_quality_gate():
    metadata = _metadata(automatic_reply=True)
    metadata["quality"]["direct_answer_precision"] = 0.98
    metadata = _approve(metadata)
    with pytest.raises(ProfessionalKnowledgeError, match="quality gate"):
        validate_article(metadata, BODY, now=NOW)


def test_professional_workbench_exposes_draft_blockers(conn, tmp_path):
    root = tmp_path / "knowledge"
    metadata = _metadata()
    metadata["status"] = "captured"
    metadata["kind"] = "unclassified"
    metadata["owner"] = None
    metadata["scope"]["basis"] = "unresolved"
    metadata["scope"]["software_versions"] = ["unresolved"]
    metadata["review"] = None
    metadata["sources"][0]["snapshot_digest"] = None
    metadata["validation"] = []
    _write_repo(root, metadata)

    snapshot = professional_knowledge_snapshot(conn, repository_root=root, now=NOW)
    codes = {item["code"] for item in snapshot["repository"]["issues"]}

    assert snapshot["ready_for_publish"] is False
    assert snapshot["database"]["legacy_approved"] == 0
    assert {
        "not_published",
        "owner_missing",
        "kind_unclassified",
        "scope_unresolved",
        "version_unresolved",
        "source_snapshot_missing",
        "claim_validation_missing",
        "review_missing",
    } <= codes
    assert "专业知识工作台" in render_professional_knowledge(snapshot)


def test_repository_lint_rejects_unknown_controlled_vocabulary(tmp_path):
    metadata = _approve(_metadata())
    root = tmp_path / "knowledge"
    _write_repo(root, metadata)
    metadata["scope"]["component"] = "invented-component"
    metadata = _approve(metadata)
    _write_repo(root, metadata)

    with pytest.raises(ProfessionalKnowledgeError, match="unknown component"):
        lint_repository(root, now=NOW)


@pytest.mark.parametrize('kind', ['article', 'directory', 'dangling', 'root'])
def test_repository_never_silently_omits_linked_articles(tmp_path, kind):
    root = tmp_path / 'knowledge'
    _write_repo(root, _approve(_metadata()))
    articles = root / 'articles'
    if kind == 'root':
        target = tmp_path / 'outside-articles'
        articles.rename(target)
        articles.symlink_to(target, target_is_directory=True)
    elif kind == 'directory':
        target = tmp_path / 'outside'
        target.mkdir()
        (articles / 'linked-directory').symlink_to(target, target_is_directory=True)
    else:
        target = next(articles.rglob('*.md')) if kind == 'article' else tmp_path / 'absent'
        (articles / 'linked.md').symlink_to(target)
    with pytest.raises(ProfessionalKnowledgeError):
        lint_repository(root, now=NOW)
    with pytest.raises(ProfessionalKnowledgeError):
        compile_repository(root, now=NOW)


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / 'knowledge' / 'articles').exists(),
    reason='Internal article corpus is not part of the public source candidate',
)
def test_checked_in_professional_repository_lints():
    root = Path(__file__).resolve().parents[1] / "knowledge"
    report = lint_repository(root, now=NOW)

    assert report["article_count"] == 17
    assert report["published_count"] == 0
    assert {article.metadata["status"] for article in report["articles"]} == {
        "structured",
        "verified",
        "needs_review",
    }
    new_revisions = [article.metadata for article in report["articles"]
                     if article.metadata["status"] == "needs_review"]
    assert len(new_revisions) == 5
    assert all(not value["publication"]["automatic_reply"] for value in new_revisions)
    environment = next(value for value in new_revisions
                       if value["id"] == "k3.uboot.environment.commands")
    assert environment["revision"] == 2
    assert environment["review"] is None
    claims = {claim["id"]: claim for claim in environment["claims"]}
    assert claims["script-execution-boundary"]["risk_class"] == "destructive"
    assert claims["runtime-environment"]["risk_class"] == "transient"
    assert {item["layer"] for item in environment["validation"]} == {"static"}


def test_git_source_verification_reads_exact_commit_not_worktree(tmp_path):
    checkout = tmp_path / "source"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "config", "user.name", "Test"], check=True
    )
    content = b"source at reviewed commit\n"
    (checkout / "cmd").mkdir()
    (checkout / "cmd" / "ufs.c").write_bytes(content)
    subprocess.run(["git", "-C", str(checkout), "add", "cmd/ufs.c"], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "source"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    metadata = _metadata()
    source = metadata["sources"][0]
    source["stable_external_id"] = f"u-boot:{commit}:cmd/ufs.c"
    source["version"] = commit
    source["snapshot_digest"] = hashlib.sha256(content).hexdigest()
    source["locator"] = {
        "repository": "u-boot",
        "commit": commit,
        "path": "cmd/ufs.c",
    }
    metadata["scope"]["software_versions"] = [commit]
    root = tmp_path / "knowledge"
    _write_repo(root, _approve(metadata))
    (checkout / "cmd" / "ufs.c").write_text("dirty worktree\n", encoding="utf-8")

    verified = verify_git_sources(root, repository="u-boot", checkout=checkout, now=NOW)
    mismatch = verify_git_sources(
        root,
        repository="u-boot",
        checkout=checkout,
        blob_reader=lambda _checkout, _commit, _path: b"different",
        now=NOW,
    )

    assert verified["ok"] is True
    assert verified["source_count"] == 1
    assert mismatch["ok"] is False
    assert mismatch["mismatch_count"] == 1


def test_compile_and_import_are_digest_gated_and_idempotent(conn, tmp_path):
    root = tmp_path / "knowledge"
    _write_repo(root, _approve(_metadata()))
    bundle = compile_repository(root, now=NOW)
    bundle_path = tmp_path / "bundle.json"
    result = write_bundle(bundle, bundle_path)
    assert bundle_path.stat().st_mode & 0o777 == 0o600
    assert result["entry_count"] == 1
    assert load_bundle(bundle_path, now=NOW)["bundle_digest"] == result["bundle_digest"]

    assert plan_import(conn, bundle_path=bundle_path, now=NOW)["actions"] == {
        "create": 1,
        "update": 0,
        "unchanged": 0,
    }
    with pytest.raises(ProfessionalKnowledgeError, match="approved digest"):
        import_bundle(
            conn,
            bundle_path=bundle_path,
            approved_digest="wrong",
            reviewer_id="owner",
            now=NOW,
        )
    imported = import_bundle(
        conn,
        bundle_path=bundle_path,
        approved_digest=result["bundle_digest"],
        reviewer_id="owner",
        now=NOW,
    )
    before_replay = conn.execute(
        """SELECT ke.updated_at,sr.last_checked_at
             FROM knowledge_entries ke CROSS JOIN source_registry sr
            WHERE ke.knowledge_id=? AND sr.stable_external_id=?""",
        (imported["knowledge_ids"][0], "uboot/uboot:k3-dev:cmd/ufs.c"),
    ).fetchone()
    repeated = import_bundle(
        conn,
        bundle_path=bundle_path,
        approved_digest=result["bundle_digest"],
        reviewer_id="owner",
        now=NOW,
    )
    after_replay = conn.execute(
        """SELECT ke.updated_at,sr.last_checked_at
             FROM knowledge_entries ke CROSS JOIN source_registry sr
            WHERE ke.knowledge_id=? AND sr.stable_external_id=?""",
        (imported["knowledge_ids"][0], "uboot/uboot:k3-dev:cmd/ufs.c"),
    ).fetchone()

    assert imported["actions"]["create"] == 1
    assert repeated["actions"]["unchanged"] == 1
    assert tuple(after_replay) == tuple(before_replay)
    row = conn.execute(
        """SELECT status,confidence,owner,professional_revision_id
             FROM knowledge_entries WHERE knowledge_id=?""",
        (imported["knowledge_ids"][0],),
    ).fetchone()
    assert dict(row) == {
        "status": "approved",
        "confidence": 0.0,
        "owner": "bootloader-team",
        "professional_revision_id": f"kvr_{bundle['entries'][0]['revision_digest'][:32]}",
    }
    assert (
        conn.execute("SELECT count(*) FROM professional_knowledge_claims").fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT count(*) FROM professional_claim_sources").fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT count(*) FROM professional_validation_runs").fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM professional_knowledge_publications"
        ).fetchone()[0]
        == 1
    )
    knowledge = dict(
        conn.execute(
            "SELECT * FROM knowledge_entries WHERE knowledge_id=?",
            (imported["knowledge_ids"][0],),
        ).fetchone()
    )
    reply = _approved_answer_markdown(conn, knowledge)
    assert "**可信范围**" in reply
    assert "K3 / u-boot / commit-1" in reply
    assert "k3.uboot.storage.inspect@r1" in reply
    assert "已验证：static" in reply


def test_import_refuses_revision_downgrade(conn, tmp_path):
    root = tmp_path / "knowledge"
    first_path = _write_repo(root, _approve(_metadata(revision=2)))
    bundle = compile_repository(root, now=NOW)
    bundle_path = tmp_path / "r2.json"
    write_bundle(bundle, bundle_path)
    import_bundle(
        conn,
        bundle_path=bundle_path,
        approved_digest=bundle["bundle_digest"],
        reviewer_id="owner",
        now=NOW,
    )

    first_path.unlink()
    _write_repo(root, _approve(_metadata(revision=1)))
    downgrade = compile_repository(root, now=NOW)
    downgrade_path = tmp_path / "r1.json"
    write_bundle(downgrade, downgrade_path)
    with pytest.raises(ProfessionalKnowledgeError, match="downgrade"):
        plan_import(conn, bundle_path=downgrade_path, now=NOW)


def test_import_keeps_revision_history_and_retires_the_previous_projection(
    conn, tmp_path
):
    root = tmp_path / "knowledge"
    first_path = _write_repo(root, _approve(_metadata(revision=1)))
    first_bundle = compile_repository(root, now=NOW)
    first_bundle_path = tmp_path / "r1.json"
    write_bundle(first_bundle, first_bundle_path)
    import_bundle(
        conn,
        bundle_path=first_bundle_path,
        approved_digest=first_bundle["bundle_digest"],
        reviewer_id="owner",
        now=NOW,
    )

    first_path.unlink()
    _write_repo(root, _approve(_metadata(revision=2)))
    second_bundle = compile_repository(root, now=NOW)
    second_bundle_path = tmp_path / "r2.json"
    write_bundle(second_bundle, second_bundle_path)
    result = import_bundle(
        conn,
        bundle_path=second_bundle_path,
        approved_digest=second_bundle["bundle_digest"],
        reviewer_id="owner",
        now=NOW,
    )

    assert result["actions"]["update"] == 1
    states = conn.execute(
        """SELECT revision_number,lifecycle_state
             FROM professional_knowledge_revisions ORDER BY revision_number"""
    ).fetchall()
    assert [tuple(row) for row in states] == [(1, "retired"), (2, "published")]


def test_document_drift_invalidates_professional_revision_without_rewriting_evidence(
    conn, tmp_path
):
    metadata = _metadata()
    metadata["sources"][0].update(
        {
            "type": "feishu_doc",
            "stable_external_id": "wiki:fan",
            "version": "1",
            "url": "https://example.feishu.cn/wiki/fan",
            "locator": {"wiki_token": "fan", "revision": "1"},
        }
    )
    root = tmp_path / "knowledge"
    _write_repo(root, _approve(metadata))
    bundle = compile_repository(root, now=NOW)
    path = tmp_path / "bundle.json"
    write_bundle(bundle, path)
    import_bundle(
        conn,
        bundle_path=path,
        approved_digest=bundle["bundle_digest"],
        reviewer_id="owner",
        now=NOW,
    )
    before = tuple(
        conn.execute(
            "SELECT revision_digest,payload_json,body_markdown FROM professional_knowledge_revisions"
        ).fetchone()
    )
    register_source(
        conn,
        source_type="feishu_doc",
        stable_external_id="wiki:fan",
        title="Changed guide",
        url="https://example.feishu.cn/wiki/fan",
        acl={"visibility": "internal"},
        source_version="2",
        content_digest="b" * 64,
        updated_at=NOW.isoformat(),
        checked_at=NOW.isoformat(),
    )
    assert conn.execute("SELECT status FROM knowledge_entries").fetchone()[0] == "stale"
    assert (
        conn.execute(
            "SELECT lifecycle_state FROM professional_knowledge_revisions"
        ).fetchone()[0]
        == "needs_review"
    )
    assert (
        tuple(
            conn.execute(
                "SELECT revision_digest,payload_json,body_markdown FROM professional_knowledge_revisions"
            ).fetchone()
        )
        == before
    )
    snapshot = professional_knowledge_snapshot(conn, repository_root=root, now=NOW)
    assert snapshot["database"]["needs_review"] == 1
    # Replaying an older approved bundle must not make the source current again.
    import_bundle(
        conn,
        bundle_path=path,
        approved_digest=bundle["bundle_digest"],
        reviewer_id="owner",
        now=NOW,
    )
    assert conn.execute("SELECT status FROM knowledge_entries").fetchone()[0] == "stale"
    assert (
        conn.execute("SELECT source_version FROM source_registry").fetchone()[0] == "2"
    )


def test_legacy_inventory_reports_missing_fields_without_guessing(conn, tmp_path):
    conn.execute(
        """INSERT INTO knowledge_entries(
               knowledge_id,title,status,question_variants_json,answer_markdown,
               project,module,software_version,disclosure_class,confidence,
               source_authority,source_digest,content_digest,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "knw_legacy",
            "Legacy",
            "approved",
            '["question"]',
            "answer",
            "K3",
            "Boot",
            "commit-a",
            "internal",
            0.95,
            0.95,
            "source",
            "content",
            "2026-09-03T00:00:00+00:00",
            "2026-09-03T00:00:00+00:00",
        ),
    )
    inventory = legacy_inventory(conn)
    assert inventory["entry_count"] == 1
    assert inventory["entries"][0]["missing_professional_fields"] == [
        "hardware",
        "applicability",
        "owner",
        "review_due_at",
        "evidence_layers",
        "source_snapshot_digest",
    ]
    output = tmp_path / "legacy.json"
    result = write_legacy_inventory(conn, output_path=output)
    assert output.stat().st_mode & 0o777 == 0o600
    assert result["inventory_digest"] == inventory["inventory_digest"]

    repository = tmp_path / "knowledge"
    (repository / "vocabulary.yaml").parent.mkdir(parents=True, exist_ok=True)
    (repository / "vocabulary.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "products": ["K3"],
                "components": ["unclassified"],
                "boards": ["board1"],
                "boot_stages": ["u-boot"],
                "storage_media": ["ufs"],
            }
        ),
        encoding="utf-8",
    )
    exported = export_legacy_drafts(conn, repository_root=repository)
    replayed = export_legacy_drafts(conn, repository_root=repository)
    assert len(exported["written"]) == 1
    assert replayed["written"] == []
    assert len(replayed["unchanged"]) == 1
    report = lint_repository(repository, now=NOW)
    article = report["articles"][0]
    assert article.metadata["status"] == "captured"
    assert article.metadata["kind"] == "unclassified"
    assert article.metadata["owner"] is None
    assert article.metadata["sources"][0]["authority"] == 0.0
