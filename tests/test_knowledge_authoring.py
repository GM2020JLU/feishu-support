import pytest
from test_docling_review import evidence

from k3_support.docling_draft import build_draft
from k3_support.knowledge_authoring import detail, page, save_attachment


def fields():
    return {
        "evidence": evidence(),
        "title": "Draft",
        "question": "Question?",
        "answer": "Unverified",
        "references": ["#/texts/0"],
        "risk_class": "read_only",
        "rollback": "",
    }


def test_reediting_saved_draft_preserves_original_and_requires_new_digest(conn):
    value = fields()
    original = build_draft(**value)
    saved = save_attachment(conn, fields=value, expected_digest=original['revision_digest'], actor_id='owner')
    before = detail(conn, candidate_id=saved['candidate_id'])
    value['answer'] = 'Revised candidate answer'
    with pytest.raises(ValueError, match='草稿已改变'):
        save_attachment(conn, fields=value, expected_digest=original['revision_digest'], actor_id='owner')
    revised = build_draft(**value)
    new = save_attachment(conn, fields=value, expected_digest=revised['revision_digest'], actor_id='owner')
    assert new['candidate_id'] != saved['candidate_id']
    assert detail(conn, candidate_id=saved['candidate_id']) == before
    assert len(page(conn)['items']) == 2
    assert new['status'] == 'captured'
    assert not new['reviewed'] and not new['automatic_reply_eligible']


def test_cli_exports_private_draft_without_changing_database(conn, config, tmp_path, capsys):
    import json
    import shutil
    from pathlib import Path

    import yaml

    from k3_support import cli

    value = fields()
    preview = build_draft(**value)
    saved = save_attachment(conn, fields=value, expected_digest=preview['revision_digest'], actor_id='owner')
    config.path.write_text(yaml.safe_dump(config.raw))
    before = conn.serialize()
    (tmp_path / 'articles').mkdir()
    output = tmp_path / 'articles' / 'review.md'
    argv = ['--config', str(config.path), 'knowledge-authoring-export',
            '--candidate-id', saved['candidate_id'], '--output', str(output)]
    assert cli.main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert not report['reviewed'] and not report['automatic_reply_eligible']
    assert output.read_text() == preview['markdown']
    assert output.stat().st_mode & 0o777 == 0o600
    from k3_support.professional_knowledge import ProfessionalKnowledgeError, compile_repository, load_article

    article = load_article(output)
    assert article.revision_digest == preview['revision_digest']
    assert article.metadata['status'] == 'captured'
    assert article.metadata['review'] is None
    assert not article.metadata['publication']['automatic_reply']
    shutil.copyfile(Path(__file__).resolve().parents[1] / 'knowledge' / 'vocabulary.yaml',
                    tmp_path / 'vocabulary.yaml')
    with pytest.raises(ProfessionalKnowledgeError, match='unknown product: unresolved'):
        compile_repository(tmp_path)
    assert conn.serialize() == before
    assert cli.main(argv) == 2
    assert 'FileExistsError' in capsys.readouterr().err
    assert output.read_text() == preview['markdown']


def test_export_rejects_symlink_output(conn, tmp_path):
    from k3_support.knowledge_authoring import export_markdown

    value = fields()
    saved = save_attachment(conn, fields=value, expected_digest=build_draft(**value)['revision_digest'], actor_id='owner')
    destination = tmp_path / 'target.md'
    destination.write_text('keep')
    link = tmp_path / 'link.md'
    link.symlink_to(destination)
    with pytest.raises(ValueError, match='symlinks'):
        export_markdown(conn, candidate_id=saved['candidate_id'], output=link)
    assert destination.read_text() == 'keep'


def test_authored_scope_changes_digest_and_stays_unreviewed(conn):
    value = fields()
    original = build_draft(**value)
    value['authored_scope'] = {'product': 'K3', 'component': 'u-boot', 'software_version': 'commit-1'}
    scoped = build_draft(**value)
    assert scoped['revision_digest'] != original['revision_digest']
    with pytest.raises(ValueError, match='草稿已改变'):
        save_attachment(conn, fields=value, expected_digest=original['revision_digest'], actor_id='owner')
    saved = save_attachment(conn, fields=value, expected_digest=scoped['revision_digest'], actor_id='owner')
    metadata = detail(conn, candidate_id=saved['candidate_id'])['metadata']
    assert metadata['scope']['product'] == 'K3'
    assert metadata['scope']['software_versions'] == ['commit-1']
    assert metadata['scope']['basis'] == 'unresolved'
    assert metadata['review'] is None and not metadata['publication']['automatic_reply']
    tasks = detail(conn, candidate_id=saved['candidate_id'])['review_tasks']
    assert not tasks['release_ready']
    assert set(tasks['items']) == {'classify_knowledge', 'assign_owner', 'verify_applicability',
        'verify_original_sources', 'validate_claims', 'review_disclosure', 'human_review',
        'evaluate_before_auto_reply'}


@pytest.mark.parametrize('scope', [[], {'product': 'K3'},
    {'product': 'K3', 'component': 'u-boot', 'software_version': 'v1', 'verified': True},
    {'product': 3, 'component': 'u-boot', 'software_version': 'v1'}])
def test_authored_scope_rejects_malformed_or_authority_fields(scope):
    with pytest.raises(ValueError):
        build_draft(**fields(), authored_scope=scope)


@pytest.mark.parametrize('column', ['markdown', 'metadata_json', 'revision_digest', 'title'])
def test_corrupt_draft_is_not_exported(conn, tmp_path, column):
    import json

    from k3_support.knowledge_authoring import export_markdown

    value = fields()
    draft = build_draft(**value)
    saved = save_attachment(conn, fields=value, expected_digest=draft['revision_digest'], actor_id='owner')
    replacements = {'markdown': draft['markdown'] + '\nchanged answer',
                    'metadata_json': json.dumps({**draft['metadata'], 'title': 'changed'}),
                    'revision_digest': '0' * 64, 'title': 'incorrect displayed title'}
    conn.execute(f'UPDATE knowledge_authoring_drafts SET {column}=?', (replacements[column],))
    output = tmp_path / 'draft.md'
    before = conn.serialize()
    with pytest.raises(ValueError, match='inconsistent'):
        detail(conn, candidate_id=saved['candidate_id'])
    with pytest.raises(ValueError, match='inconsistent'):
        export_markdown(conn, candidate_id=saved['candidate_id'], output=output)
    assert not output.exists()
    assert conn.serialize() == before


def test_durable_draft_is_deduplicated_and_not_published(conn):
    value = fields()
    preview = build_draft(**value)
    receipt = save_attachment(
        conn, fields=value, expected_digest=preview["revision_digest"], actor_id="owner"
    )
    again = save_attachment(
        conn, fields=value, expected_digest=preview["revision_digest"], actor_id="owner"
    )
    assert again["replayed"] and receipt["candidate_id"] == again["candidate_id"]
    stored = detail(conn, candidate_id=receipt["candidate_id"])
    assert stored["markdown"] == preview["markdown"]
    assert stored["metadata"]["review"] is None
    assert stored["material"]["status"] == "unreviewed"
    assert stored["saved_by"] == "owner"
    assert stored["automatic_reply_eligible"] is False
    assert page(conn)["items"][0]["candidate_id"] == receipt["candidate_id"]
    for table in (
        "knowledge_entries",
        "professional_knowledge_publications",
        "outbox",
        "jobs",
    ):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize('change', ['document', 'source', 'parser', 'authority', 'malformed'])
def test_review_rejects_changed_material_without_repairing_it(conn, tmp_path, change):
    import json
    from k3_support.knowledge_authoring import export_markdown

    value = fields()
    saved = save_attachment(conn, fields=value,
                            expected_digest=build_draft(**value)['revision_digest'], actor_id='owner')
    material = detail(conn, candidate_id=saved['candidate_id'])['material']
    if change == 'document':
        material['document']['texts'][0]['text'] = 'unrelated attachment contents'
    elif change == 'source':
        material['source']['version'] = 'another-version'
    elif change == 'parser':
        material['parser']['version'] = 'another-parser'
    elif change == 'authority':
        material['automatic_reply_eligible'] = True
    else:
        material = []
    conn.execute('UPDATE knowledge_authoring_drafts SET material_json=?', (json.dumps(material),))
    before = conn.serialize()
    with pytest.raises(ValueError, match='material is inconsistent'):
        detail(conn, candidate_id=saved['candidate_id'])
    output = tmp_path / 'invalid.md'
    with pytest.raises(ValueError, match='material is inconsistent'):
        export_markdown(conn, candidate_id=saved['candidate_id'], output=output)
    assert not output.exists()
    assert conn.serialize() == before


def test_stale_draft_cannot_save(conn):
    value = fields()
    preview = build_draft(**value)
    value["answer"] = "changed"
    with pytest.raises(ValueError, match="改变"):
        save_attachment(
            conn,
            fields=value,
            expected_digest=preview["revision_digest"],
            actor_id="owner",
        )
    assert page(conn)["items"] == []


@pytest.mark.parametrize('column,replacement', [
    ('markdown', 'broken frontmatter'), ('material_json', '{}'),
    ('title', 'wrong title'), ('revision_digest', '0' * 64),
])
def test_duplicate_save_rejects_corrupt_existing_candidate(conn, column, replacement):
    value = fields()
    revision = build_draft(**value)['revision_digest']
    save_attachment(conn, fields=value, expected_digest=revision, actor_id='original-owner')
    conn.execute(f'UPDATE knowledge_authoring_drafts SET {column}=?', (replacement,))
    before = conn.serialize()
    with pytest.raises(ValueError):
        save_attachment(conn, fields=value, expected_digest=revision, actor_id='retry-owner')
    assert conn.serialize() == before
    assert conn.execute('SELECT saved_by FROM knowledge_authoring_drafts').fetchone()[0] == 'original-owner'


def test_injected_approval_is_rejected(conn):
    value = fields()
    value["review"] = {"approved": True}
    with pytest.raises(ValueError):
        save_attachment(conn, fields=value, expected_digest="x", actor_id="owner")
    assert page(conn)["items"] == []


@pytest.mark.parametrize('target,key,value', [
    ('parser', 'metadata_verified', True), ('parser', 'name', 'trusted-parser'),
    ('source', 'reviewed', True), (None, 'reviewed', True)])
def test_saved_material_cannot_display_forged_authority(conn, target, key, value):
    import json
    inputs = fields()
    receipt = save_attachment(conn, fields=inputs,
        expected_digest=build_draft(**inputs)['revision_digest'], actor_id='owner')
    material = json.loads(conn.execute('SELECT material_json FROM knowledge_authoring_drafts').fetchone()[0])
    (material[target] if target else material)[key] = value
    conn.execute('UPDATE knowledge_authoring_drafts SET material_json=?', (json.dumps(material),))
    before = conn.serialize()
    with pytest.raises(ValueError, match='material is inconsistent'):
        detail(conn, candidate_id=receipt['candidate_id'])
    assert conn.serialize() == before


def test_upgrade_preserves_existing_knowledge_and_saved_draft_survives_reopen(
    tmp_path, monkeypatch
):
    from k3_support import db
    from k3_support.knowledge import create_candidate

    migrations = db.migration_files()
    monkeypatch.setattr(
        db, "migration_files", lambda: [m for m in migrations if m[0] <= 85]
    )
    path = tmp_path / "upgrade.db"
    conn = db.connect(path)
    try:
        db.migrate(conn)
        create_candidate(
            conn,
            title="old",
            questions=["q"],
            answer_markdown="a",
            project=None,
            module=None,
            software_version=None,
            disclosure_class="private",
            confidence=0,
            source_authority=0,
            canonical_case_id=None,
            source_digest="old",
        )
        before = [tuple(r) for r in conn.execute("SELECT * FROM knowledge_entries")]
        monkeypatch.setattr(db, "migration_files", lambda: migrations)
        assert db.migrate(conn) == [version for version, _, _ in migrations if version > 85]
        assert [
            tuple(r) for r in conn.execute("SELECT * FROM knowledge_entries")
        ] == before
        value = fields()
        receipt = save_attachment(
            conn,
            fields=value,
            expected_digest=build_draft(**value)["revision_digest"],
            actor_id="owner",
        )
        assert db.migrate(conn) == []
    finally:
        conn.close()
    conn = db.connect(path)
    try:
        assert detail(conn, candidate_id=receipt["candidate_id"])["saved_by"] == "owner"
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
    finally:
        conn.close()
