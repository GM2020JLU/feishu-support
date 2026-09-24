import json

import pytest
from test_knowledge_feedback import approved_knowledge

from k3_support.ids import digest
from k3_support.knowledge_use_preview import preview
from k3_support.store import create_case, enqueue_outbox


def test_retired_sent_reply_is_rejected_before_payload_read(conn):
    import sqlite3
    from k3_support.content_retirement import ContentRetiredError
    case, _, outbox, _ = seed_sent_reply(conn)
    round_number = conn.execute('SELECT lifecycle_round FROM outbox WHERE outbox_id=?', (outbox,)).fetchone()[0]
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (case, round_number, 'sent-retired', 'a'*64, 'b'*64, 'now', 'fixture'))
    def no_payload(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and table == 'outbox' and column == 'payload_json':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(no_payload)
    try:
        with pytest.raises(ContentRetiredError):
            preview(conn, case_id=case, use_id='use-1')
    finally:
        conn.set_authorizer(None)


def test_sent_reply_preview_keeps_retirement_check_inside_snapshot(conn, monkeypatch):
    from k3_support import content_retirement
    case, _, _, _ = seed_sent_reply(conn)
    original = content_retirement.require_case_content
    calls = []
    def checked(*args, **kwargs):
        calls.append(conn.in_transaction)
        return original(*args, **kwargs)
    monkeypatch.setattr(content_retirement,'require_case_content',checked)
    before = conn.serialize()
    assert not conn.in_transaction
    assert preview(conn,case_id=case,use_id='use-1')['version_bound']
    assert calls == [True] and not conn.in_transaction
    assert conn.serialize() == before


def seed_sent_reply(conn, bound=True):
    case, _ = create_case(conn, title="fan", case_type="faq", severity="P3", confidence=0.9)
    knowledge = approved_knowledge(conn, case)
    payload = {"text": "original sent answer"}
    if bound:
        payload["knowledge_release"] = {"knowledge_ids": [knowledge],
            "provenance": {"knowledge_entry_fingerprint": "a" * 64},
            "text_digest": digest(payload["text"])}
    outbox, _ = enqueue_outbox(conn, channel="feishu_im", action_type="reply",
        destination="fixture", payload=payload, idempotency_key="preview", case_id=case)
    conn.execute("UPDATE outbox SET state='delivered',remote_message_id='sent' WHERE outbox_id=?", (outbox,))
    conn.execute("INSERT INTO knowledge_uses VALUES('use-1',?,?,?,'delivered','now','now')",
                 (knowledge, case, outbox))
    return case, knowledge, outbox, payload


@pytest.mark.parametrize("bound", [True, False])
def test_exact_sent_reply_preview_is_readonly_and_rejects_changed_receipt(conn, bound):
    case, _knowledge, outbox, payload = seed_sent_reply(conn, bound)
    before = conn.serialize()
    shown = preview(conn, case_id=case, use_id="use-1")
    assert shown["version_bound"] is bound
    assert shown["sent_text"] == "original sent answer"
    assert not shown["automatic_publication"]
    assert conn.serialize() == before
    with pytest.raises(ValueError):
        preview(conn, case_id="other", use_id="use-1")
    payload["text"] = "changed"
    conn.execute("UPDATE outbox SET payload_json=? WHERE outbox_id=?", (json.dumps(payload), outbox))
    with pytest.raises(ValueError, match="已变化"):
        preview(conn, case_id=case, use_id="use-1", expected_digest=shown["content_digest"])
    assert not preview(conn, case_id=case, use_id="use-1")["version_bound"]
