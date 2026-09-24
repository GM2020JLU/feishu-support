from datetime import UTC, datetime

import pytest
from test_knowledge_authoring import fields

from k3_support.docling_draft import build_draft
from k3_support.draft_retention import clear, preview
from k3_support.knowledge_authoring import save_attachment


def seed(conn):
    value = fields()
    draft = build_draft(**value)
    saved = save_attachment(conn, fields=value, expected_digest=draft['revision_digest'], actor_id='owner')
    conn.execute("UPDATE knowledge_authoring_drafts SET saved_at='2020-01-01T00:00:00+00:00'")
    return saved['candidate_id']


def test_expired_capture_clear_is_atomic_and_audited(conn):
    identifier = seed(conn)
    before = conn.serialize()
    value = preview(conn, candidate_id=identifier, days=30)
    assert value['eligible'] and conn.serialize() == before
    result = clear(conn, candidate_id=identifier, days=30, expected_digest=value['row_digest'], actor_id='owner')
    assert not result['secure_erasure']
    assert not conn.execute('SELECT 1 FROM knowledge_authoring_drafts').fetchone()
    assert conn.execute('SELECT artifact_hash FROM retention_tombstones').fetchone()[0] == value['row_digest']
    with pytest.raises(ValueError):
        clear(conn, candidate_id=identifier, days=30, expected_digest=value['row_digest'], actor_id='owner')


@pytest.mark.parametrize('change', ['content', 'reference', 'age', 'days'])
def test_stale_clear_is_rejected_without_deleting(conn, change):
    identifier = seed(conn)
    value = preview(conn, candidate_id=identifier, days=30)
    if change == 'content':
        conn.execute("UPDATE knowledge_authoring_drafts SET markdown='changed'")
    elif change == 'reference':
        conn.execute('CREATE TABLE synthetic_reference(payload_json TEXT)')
        conn.execute('INSERT INTO synthetic_reference VALUES(json_object(?,?))', ('draft', identifier))
    elif change == 'age':
        conn.execute('UPDATE knowledge_authoring_drafts SET saved_at=?', (datetime.now(UTC).isoformat(),))
    before = conn.serialize()
    with pytest.raises(ValueError):
        clear(conn, candidate_id=identifier, days=1 if change == 'days' else 30,
              expected_digest=value['row_digest'], actor_id='owner')
    assert conn.serialize() == before


def test_audit_and_deletion_rollback_together(conn):
    import sqlite3

    identifier = seed(conn)
    value = preview(conn, candidate_id=identifier, days=30)
    conn.execute("""CREATE TRIGGER synthetic_delete_failure BEFORE DELETE ON knowledge_authoring_drafts
                    BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
    before = conn.serialize()
    with pytest.raises(sqlite3.IntegrityError):
        clear(conn, candidate_id=identifier, days=30, expected_digest=value['row_digest'], actor_id='owner')
    assert conn.serialize() == before
    assert not conn.execute('SELECT 1 FROM retention_tombstones').fetchone()


@pytest.mark.parametrize('column', ['metadata_json', 'material_json'])
@pytest.mark.parametrize('shape', ['nested_value', 'map_key'])
def test_other_draft_reference_in_any_json_field_prevents_delete(conn, column, shape):
    import json
    identifier = seed(conn)
    original = preview(conn, candidate_id=identifier, days=30)
    value = fields()
    value['title'] = 'Separate draft'
    value['answer'] = 'Another answer'
    draft = build_draft(**value)
    other = save_attachment(conn, fields=value, expected_digest=draft['revision_digest'], actor_id='owner')['candidate_id']
    row = json.loads(conn.execute(f'SELECT {column} FROM knowledge_authoring_drafts WHERE candidate_id=?', (other,)).fetchone()[0])
    row['related'] = {'deep': [identifier]} if shape == 'nested_value' else {identifier: 'reference'}
    conn.execute(f'UPDATE knowledge_authoring_drafts SET {column}=? WHERE candidate_id=?', (json.dumps(row), other))
    assert not preview(conn, candidate_id=identifier, days=30)['eligible']
    with pytest.raises(ValueError):
        clear(conn, candidate_id=identifier, days=30, expected_digest=original['row_digest'], actor_id='owner')
    assert conn.execute('SELECT 1 FROM knowledge_authoring_drafts WHERE candidate_id=?', (identifier,)).fetchone()
