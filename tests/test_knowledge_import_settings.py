import copy
import sqlite3

import pytest
from test_professional_knowledge import BODY, _approve, _metadata, _write_repo

from k3_support.ids import digest
from k3_support.knowledge_import_settings import apply, preview
from k3_support.professional_knowledge import compile_repository, revision_digest


def multi_bundle(metadata):
    entries = [
        {
            "metadata": _approve(item),
            "body_markdown": BODY,
            "revision_digest": revision_digest(_approve(item), BODY),
        }
        for item in metadata
    ]
    payload = {"schema_version": 2, "entries": entries}
    return {**payload, "bundle_digest": digest(payload)}


@pytest.mark.parametrize("revisions", [(1, 2), (2, 1), (1, 1)])
def test_multiple_revisions_of_one_article_rejected_before_staging(conn, revisions):
    with pytest.raises(ValueError, match="one revision"):
        preview(
            conn,
            bundle=multi_bundle([_metadata(revision=i) for i in revisions]),
            session_id="session",
        )
    assert (
        conn.execute("SELECT count(*) FROM knowledge_import_drafts").fetchone()[0] == 0
    )


def test_conflicting_source_snapshots_rejected(conn):
    first, second = _metadata(), _metadata(revision=2)
    second["id"] = "k3.uboot.storage.second"
    with pytest.raises(ValueError, match="conflicting source"):
        preview(conn, bundle=multi_bundle([first, second]), session_id="session")


def test_multi_article_failure_rolls_back_and_same_draft_can_be_retried(conn):
    first = _metadata()
    second = copy.deepcopy(first)
    second["id"] = "k3.uboot.storage.second"
    pending = preview(conn, bundle=multi_bundle([first, second]), session_id="session")
    conn.execute(
        "CREATE TEMP TRIGGER reject_second BEFORE INSERT ON professional_knowledge_revisions WHEN NEW.stable_id='k3.uboot.storage.second' BEGIN SELECT RAISE(ABORT,'second failed'); END"
    )
    before = list(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="second failed"):
        apply(
            conn, draft_id=pending["draft_id"], session_id="session", actor_id="owner"
        )
    assert list(conn.iterdump()) == before
    conn.execute("DROP TRIGGER reject_second")
    result = apply(
        conn, draft_id=pending["draft_id"], session_id="session", actor_id="owner"
    )
    assert result["actions"]["create"] == 2


def bundle(tmp_path):
    _write_repo(tmp_path, _approve(_metadata()))
    return compile_repository(tmp_path)


def test_import_preview_explicit_apply_and_replay(conn, tmp_path):
    from k3_support.audit_inventory import page

    pending = preview(conn, bundle=bundle(tmp_path), session_id="session")
    assert page(conn, kind="knowledge_import")["total_matching"] == 0
    assert pending["changes"][0]["action"] == "create"
    assert pending["changes"][0]["previous"] is None
    assert conn.execute("SELECT count(*) FROM knowledge_entries").fetchone()[0] == 0
    with pytest.raises(ValueError):
        apply(conn, draft_id=pending["draft_id"], session_id="other", actor_id="owner")
    result = apply(
        conn, draft_id=pending["draft_id"], session_id="session", actor_id="owner"
    )
    assert result["actions"]["create"] == 1
    assert apply(
        conn, draft_id=pending["draft_id"], session_id="session", actor_id="owner"
    )["replayed"]
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    before = list(conn.iterdump())
    audit = page(conn, kind="knowledge_import")
    assert audit["total_matching"] == 1
    assert audit["items"][0]["target"] == pending["draft_id"]
    assert audit["items"][0]["actor_id"] == "owner"
    assert "不代表自动回复已授权" in audit["items"][0]["summary"]
    assert "bundle_json" not in str(audit) and "session" not in str(audit)
    assert list(conn.iterdump()) == before


def test_preview_shows_old_body_scope_and_status_without_republishing(conn):
    pending = preview(conn, bundle=multi_bundle([_metadata()]), session_id="session")
    apply(conn, draft_id=pending["draft_id"], session_id="session", actor_id="owner")
    conn.execute("UPDATE knowledge_entries SET status='retired'")
    before = list(conn.execute("SELECT * FROM knowledge_entries"))
    next_preview = preview(
        conn, bundle=multi_bundle([_metadata(revision=2)]), session_id="session"
    )
    change = next_preview["changes"][0]
    assert change["action"] == "update" and change["proposed_revision"] == 2
    assert change["previous"]["metadata"]["revision"] == 1
    assert change["previous"]["body_markdown"] == BODY
    assert change["previous"]["entry_status"] == "retired"
    assert change["previous"]["metadata"]["scope"] == _metadata()["scope"]
    assert list(conn.execute("SELECT * FROM knowledge_entries")) == before


@pytest.mark.parametrize("change", ["expiry", "source"])
def test_changed_state_or_expired_import_rejected(conn, tmp_path, change):
    pending = preview(conn, bundle=bundle(tmp_path), session_id="session")
    if change == "expiry":
        conn.execute(
            "UPDATE knowledge_import_drafts SET expires_at='2000-01-01T00:00:00+00:00'"
        )
    else:
        from k3_support.knowledge import register_source

        register_source(
            conn,
            source_type="doc",
            stable_external_id="changed",
            title="changed",
            url=None,
            acl={"visibility": "private"},
            source_version=None,
            content_digest=None,
            updated_at=None,
        )
    with pytest.raises(ValueError):
        apply(
            conn, draft_id=pending["draft_id"], session_id="session", actor_id="owner"
        )
    assert conn.execute("SELECT count(*) FROM knowledge_entries").fetchone()[0] == 0
