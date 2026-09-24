from test_knowledge_runtime import entry

from k3_support import knowledge_corpus as corpus
from k3_support.db import transaction


def test_build_is_full_corpus_and_mutation_invalidates_without_authorizing(conn):
    first, second = entry(conn, 'first'), entry(conn, 'second')
    built = corpus.build(conn)
    assert built['entry_count'] == 2 and not built['release_authorized']
    assert corpus.state(conn)['current']
    conn.execute("UPDATE knowledge_entries SET disclosure_class='restricted' WHERE knowledge_id=?", (first,))
    assert not corpus.state(conn)['current']
    assert corpus.build(conn)['corpus_digest'] != built['corpus_digest']
    conn.execute("UPDATE knowledge_entries SET status='retired' WHERE knowledge_id=?", (second,))
    assert not corpus.state(conn)['current']


def test_revision_updates_rollback_with_content(conn):
    key = entry(conn)
    before = corpus.state(conn)['revision']
    try:
        with transaction(conn):
            conn.execute("UPDATE knowledge_entries SET title='changed' WHERE knowledge_id=?", (key,))
            assert corpus.state(conn)['revision'] > before
            raise RuntimeError('abort')
    except RuntimeError:
        pass
    assert corpus.state(conn)['revision'] == before


def test_changed_during_build_cannot_mark_old_snapshot_current(conn, monkeypatch):
    key = entry(conn)
    original = corpus.corpus_fingerprint
    def mutate(rows):
        assert not conn.in_transaction
        conn.execute("UPDATE knowledge_entries SET title='new' WHERE knowledge_id=?", (key,))
        return original(rows)
    monkeypatch.setattr(corpus, 'corpus_fingerprint', mutate)
    assert corpus.build(conn)['reason'] == 'corpus_changed'
    assert not corpus.state(conn)['current']
    assert conn.execute('SELECT count(*) FROM knowledge_corpus_builds').fetchone()[0] == 0


def test_metadata_pages_exclude_answers_and_keep_full_corpus_digest(conn):
    keys = [entry(conn, title) for title in ('first', 'second', 'third')]
    for key in keys:
        conn.execute('UPDATE knowledge_entries SET answer_markdown=? WHERE knowledge_id=?', ('PRIVATE-BODY-MARKER', key))
    built = corpus.build(conn)
    page = corpus.metadata_page(conn, limit=1)
    assert len(page['items']) == 1 and page['next_cursor']
    assert 'PRIVATE-BODY-MARKER' not in str(page)
    assert 'answer_markdown' not in page['items'][0]
    following = corpus.metadata_page(conn, after_id=page['next_cursor'], limit=1)
    assert following['corpus_digest'] == page['corpus_digest'] == built['corpus_digest']
    assert following['items'][0]['knowledge_id'] != page['items'][0]['knowledge_id']
    conn.execute("UPDATE knowledge_entries SET disclosure_class='restricted' WHERE knowledge_id=?", (keys[0],))
    import pytest
    with pytest.raises(ValueError, match='not current'):
        corpus.metadata_page(conn)


def test_source_acl_and_deletion_invalidate_full_source_binding(conn):
    entry(conn)
    conn.execute("INSERT INTO source_registry(source_id,source_type,stable_external_id,acl_json) VALUES('source','doc','doc-id','{}')")
    first = corpus.build(conn)
    conn.execute('UPDATE source_registry SET acl_json=? WHERE source_id=?', ('{"visibility":"restricted"}', 'source'))
    assert not corpus.state(conn)['current']
    second = corpus.build(conn)
    assert second['corpus_digest'] == first['corpus_digest']
    assert second['sources_digest'] != first['sources_digest']
    conn.execute("DELETE FROM source_registry WHERE source_id='source'")
    assert not corpus.state(conn)['current']


def test_revision_retirement_and_publication_changes_invalidate(conn):
    from test_knowledge_runtime import professional
    key = professional(conn)
    revision = conn.execute('SELECT professional_revision_id FROM knowledge_entries WHERE knowledge_id=?', (key,)).fetchone()[0]
    corpus.build(conn)
    conn.execute("UPDATE professional_knowledge_revisions SET lifecycle_state='retired' WHERE revision_id=?", (revision,))
    assert not corpus.state(conn)['current']
    corpus.build(conn)
    conn.execute('''INSERT INTO professional_knowledge_publications VALUES(?,?,?,?,?,?)''',
                 ('pub', revision, 'a'*64, 'b'*64, 'synthetic', '2026-09-09'))
    assert not corpus.state(conn)['current']


def test_usage_counters_do_not_invalidate_content_generation(conn):
    key = entry(conn)
    corpus.build(conn)
    before = corpus.state(conn)['revision']
    conn.execute('UPDATE knowledge_entries SET use_count=use_count+1,updated_at=? WHERE knowledge_id=?', ('later', key))
    assert corpus.state(conn)['revision'] == before
    assert corpus.state(conn)['current']
