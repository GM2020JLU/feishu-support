import uuid

import pytest
from test_knowledge_preview import candidate

from k3_support.knowledge_lifecycle import apply
from k3_support.knowledge_preview import knowledge_preview


def request(conn, identifier, decision="retired"):
    return {
        "knowledge_id": identifier,
        "decision": decision,
        "actor_id": "owner",
        "request_id": str(uuid.uuid4()),
        "content_digest": knowledge_preview(conn, knowledge_id=identifier)["preview"][
            "content_digest"
        ],
    }


def test_withdrawal_audit_and_replay_do_not_reapply_old_state(conn):
    identifier = candidate(conn)
    first = request(conn, identifier)
    assert not apply(conn, **first)["published"]
    audit = conn.execute("SELECT * FROM knowledge_lifecycle_actions").fetchone()
    assert audit["previous_status"] == "candidate" and audit["actor_id"] == "owner"
    apply(conn, **request(conn, identifier, "candidate"))
    assert apply(conn, **first)["replayed"]
    assert (
        conn.execute("SELECT status FROM knowledge_entries").fetchone()[0]
        == "candidate"
    )
    assert (
        conn.execute("SELECT count(*) FROM knowledge_lifecycle_actions").fetchone()[0]
        == 2
    )
    with pytest.raises(ValueError, match="其他操作"):
        apply(conn, **{**first, "actor_id": "other"})


def test_professional_revision_withdrawal_preserves_publication_gate(conn, tmp_path):
    from test_professional_knowledge import NOW, _approve, _metadata, _write_repo

    from k3_support.knowledge import review
    from k3_support.professional_knowledge import (
        compile_repository,
        import_bundle,
        write_bundle,
    )

    root = tmp_path / "knowledge"
    _write_repo(root, _approve(_metadata()))
    bundle = compile_repository(root, now=NOW)
    path = tmp_path / "bundle.json"
    written = write_bundle(bundle, path)
    imported = import_bundle(
        conn,
        bundle_path=path,
        approved_digest=written["bundle_digest"],
        reviewer_id="owner",
        now=NOW,
    )
    identifier = imported["knowledge_ids"][0]
    apply(conn, **request(conn, identifier))
    assert (
        conn.execute(
            "SELECT lifecycle_state FROM professional_knowledge_revisions"
        ).fetchone()[0]
        == "retired"
    )
    apply(conn, **request(conn, identifier, "candidate"))
    assert (
        conn.execute(
            "SELECT lifecycle_state FROM professional_knowledge_revisions"
        ).fetchone()[0]
        == "needs_review"
    )
    with pytest.raises(ValueError, match="reviewed new revision"):
        review(conn, knowledge_id=identifier, reviewer_id="owner", decision="approved")


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE knowledge_entries SET answer_markdown='changed'",
        "UPDATE knowledge_sources SET claim='changed'",
    ],
)
def test_old_content_binding_rejects_without_audit_or_state_change(conn, change):
    identifier = candidate(conn)
    payload = request(conn, identifier)
    conn.execute(change)
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        apply(conn, **payload)
    assert list(conn.iterdump()) == before


def test_publish_not_available_and_failed_review_rolls_back_audit(conn):
    identifier = candidate(conn)
    payload = request(conn, identifier)
    with pytest.raises(ValueError):
        apply(conn, **{**payload, "decision": "approved"})
    conn.execute(
        "CREATE TEMP TRIGGER fail_review BEFORE UPDATE ON knowledge_entries BEGIN SELECT RAISE(ABORT,'injected failure'); END"
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        apply(conn, **payload)
    assert (
        conn.execute("SELECT count(*) FROM knowledge_lifecycle_actions").fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT status FROM knowledge_entries").fetchone()[0]
        == "candidate"
    )
