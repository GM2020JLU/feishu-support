import hashlib
from datetime import UTC, datetime

import pytest
from test_knowledge_authoring import fields

from k3_support.docling_draft import build_draft
from k3_support.knowledge_authoring import save_attachment
from k3_support.operations import apply_retention, retention_preview
from k3_support.store import ingest_event


@pytest.mark.parametrize('binding', ['event_pk', 'external_id', 'path', 'hash', 'unrelated'])
def test_captured_draft_saved_after_preview_preserves_its_source(conn, config, binding):
    directory = config.data_dir / 'attachments' / 'draft-source'
    directory.mkdir(parents=True)
    path = directory / 'raw.txt'
    path.write_text('synthetic original')
    event_pk, _ = ingest_event(conn, source='feishu_bot_im', identity='bot',
        external_id='draft-source-event', payload={'content': 'fixture'},
        occurred_at='2026-06-01T00:00:00+00:00', raw_artifact_path=str(path))
    conn.execute("UPDATE inbound_events SET received_at='2026-06-01T00:00:00+00:00' WHERE event_pk=?", (event_pk,))
    before = retention_preview(conn, config, now=datetime(2026, 9, 1, tzinfo=UTC))
    assert before[0]['status'] == 'delete'
    value = fields()
    value['evidence']['source']['id'] = {'event_pk': event_pk, 'external_id': 'draft-source-event',
                                       'path': str(path), 'hash': 'opaque-source', 'unrelated': 'other'}[binding]
    if binding == 'hash':
        value['evidence']['source']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    draft = build_draft(**value)
    save_attachment(conn, fields=value, expected_digest=draft['revision_digest'], actor_id='owner')
    current = retention_preview(conn, config, now=datetime(2026, 9, 1, tzinfo=UTC))
    related = binding != 'unrelated'
    assert current[0]['status'] == ('held' if related else 'delete')
    result = apply_retention(conn, config, before)
    assert result['held'] == int(related)
    assert path.exists() == related
    assert conn.execute('SELECT count(*) FROM knowledge_authoring_drafts').fetchone()[0] == 1


@pytest.mark.parametrize('kind', ['oversized', 'symlink', 'changed'])
def test_hash_provenance_uncertainty_holds_file(conn, tmp_path, monkeypatch, kind):
    from k3_support import operations

    value = fields()
    draft = build_draft(**value)
    save_attachment(conn, fields=value, expected_digest=draft['revision_digest'], actor_id='owner')
    path = tmp_path / 'original'
    path.write_bytes(b'old')
    if kind == 'oversized':
        monkeypatch.setattr(operations, '_RETENTION_HASH_MAX_BYTES', 2)
    elif kind == 'symlink':
        target = tmp_path / 'target'
        target.write_bytes(b'secret')
        path.unlink()
        path.symlink_to(target)
    else:
        original = hashlib.sha256
        class ReplacingHash:
            def __init__(self):
                self.inner = original()
            def update(self, chunk):
                self.inner.update(chunk)
                replacement = tmp_path / 'replacement'
                replacement.write_bytes(b'new')
                replacement.replace(path)
            def hexdigest(self):
                return self.inner.hexdigest()
        monkeypatch.setattr(operations.hashlib, 'sha256', ReplacingHash)
    assert operations._draft_hash_referenced(conn, path)
