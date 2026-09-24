from uuid import uuid4

import pytest
from test_knowledge_use_preview import seed_sent_reply

from k3_support.knowledge_sent_feedback import apply
from k3_support.knowledge_use_preview import preview


def test_upgrade_preserves_delivered_reply_and_does_not_invent_feedback(tmp_path):
    from k3_support.db import connect, integrity, migrate, migration_files

    conn = connect(tmp_path / "feedback-upgrade.db")
    try:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL)")
        for version, name, sql in migration_files():
            if version > 82:
                break
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (version, name, "then"))
        seed_sent_reply(conn)
        tables = ["cases", "knowledge_entries", "knowledge_uses", "outbox"]
        before = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in tables}
        assert migrate(conn) == [version for version, _, _ in migration_files() if version > 82]
        assert conn.execute('SELECT count(*) FROM knowledge_authoring_drafts').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 0
        assert migrate(conn) == []
        assert integrity(conn)["ok"]
        assert before == {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in tables}
        assert conn.execute("SELECT count(*) FROM sent_knowledge_feedback").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sent_feedback_reviews").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("verdict", ["helpful", "incorrect", "incomplete"])
def test_exact_feedback_dedupes_without_modifying_new_knowledge(conn, verdict):
    case, knowledge, _, _ = seed_sent_reply(conn)
    shown = preview(conn, case_id=case, use_id="use-1")
    conn.execute("UPDATE knowledge_entries SET answer_markdown='new revision' WHERE knowledge_id=?", (knowledge,))
    before = tuple(conn.execute("SELECT * FROM knowledge_entries WHERE knowledge_id=?", (knowledge,)).fetchone())
    args = {"case_id": case, "use_id": "use-1", "actor_id": "owner", "verdict": verdict,
            "content_digest": shown["content_digest"], "request_id": str(uuid4())}
    assert apply(conn, **args)["created"]
    assert not apply(conn, **args)["created"]
    assert not apply(conn, **{**args, "request_id": str(uuid4())})["created"]
    row = conn.execute("SELECT * FROM sent_knowledge_feedback").fetchone()
    assert row["entry_fingerprint"] == "a" * 64
    assert row["review_state"] == ("recorded" if verdict == "helpful" else "pending")
    from k3_support.knowledge_sent_feedback import pending

    before_read = conn.serialize()
    listed = pending(conn, actor_id="owner")
    assert len(listed["items"]) == (0 if verdict == "helpful" else 1)
    assert not pending(conn, actor_id="another")["items"]
    assert conn.serialize() == before_read
    assert conn.execute("SELECT count(*) FROM sent_knowledge_feedback").fetchone()[0] == 1
    assert tuple(conn.execute("SELECT * FROM knowledge_entries WHERE knowledge_id=?", (knowledge,)).fetchone()) == before
    with pytest.raises(ValueError):
        apply(conn, **{**args, "actor_id": "different-owner"})
    with pytest.raises(ValueError):
        apply(conn, **{**args, "case_id": "other"})


@pytest.mark.parametrize("failure", ["legacy", "stale"])
def test_unbound_or_changed_reply_creates_no_feedback(conn, failure):
    case, _, outbox, _ = seed_sent_reply(conn, bound=failure != "legacy")
    shown = preview(conn, case_id=case, use_id="use-1")
    if failure == "stale":
        conn.execute("UPDATE outbox SET remote_message_id='changed' WHERE outbox_id=?", (outbox,))
    before = conn.serialize()
    with pytest.raises(ValueError):
        apply(conn, case_id=case, use_id="use-1", actor_id="owner", verdict="incorrect",
              content_digest=shown["content_digest"], request_id=str(uuid4()))
    assert conn.serialize() == before


@pytest.mark.parametrize("decision", ["needs_revision", "dismissed"])
def test_review_is_owner_bound_idempotent_and_does_not_publish(conn, decision):
    from k3_support.knowledge_sent_feedback import pending, review_feedback

    case, knowledge, _, _ = seed_sent_reply(conn)
    shown = preview(conn, case_id=case, use_id="use-1")
    apply(conn, case_id=case, use_id="use-1", actor_id="owner", verdict="incorrect",
          content_digest=shown["content_digest"], request_id=str(uuid4()))
    item = pending(conn, actor_id="owner")["items"][0]
    args = {"feedback_id": item["request_id"], "actor_id": "owner", "decision": decision,
            "reason": "checked historical reply", "content_digest": item["review_digest"], "request_id": str(uuid4())}
    before = tuple(conn.execute("SELECT * FROM knowledge_entries WHERE knowledge_id=?", (knowledge,)).fetchone())
    with pytest.raises(ValueError):
        review_feedback(conn, **{**args, "actor_id": "other"})
    assert review_feedback(conn, **args)["created"]
    assert not review_feedback(conn, **args)["created"]
    assert not pending(conn, actor_id="owner")["items"]
    tracked = pending(conn, actor_id="owner", state=decision)["items"]
    assert len(tracked) == 1 and tracked[0]["reason"] == args["reason"]
    assert tracked[0]["decision"] == decision
    assert not pending(conn, actor_id="other", state=decision)["items"]
    assert tuple(conn.execute("SELECT * FROM knowledge_entries WHERE knowledge_id=?", (knowledge,)).fetchone()) == before
    assert conn.execute("SELECT decision FROM sent_feedback_reviews").fetchone()[0] == decision
    from k3_support.knowledge_sent_feedback import revision_material

    before_material = conn.serialize()
    if decision == "needs_revision":
        material = revision_material(conn, feedback_id=item["request_id"], actor_id="owner")
        assert material["sent_answer"] == "original sent answer"
        assert not material["question_available"]
        assert not material["gold_approved"] and not material["publication_allowed"]
        assert material["expected_answer"] is None
        assert material["sent_entry_fingerprint"] == "a" * 64
    else:
        with pytest.raises(ValueError):
            revision_material(conn, feedback_id=item["request_id"], actor_id="owner")
    with pytest.raises(ValueError):
        revision_material(conn, feedback_id=item["request_id"], actor_id="other")
    assert conn.serialize() == before_material
