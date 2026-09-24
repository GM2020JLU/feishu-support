import pytest
from test_knowledge_runtime import entry

from k3_support.knowledge_runtime import candidate_rows, corpus_rows, RetrievalError


def test_candidate_rows_match_corpus_without_loading_unselected_bodies(conn):
    first = entry(conn, 'first')
    entry(conn, 'unselected private body')
    expected = [row for row in corpus_rows(conn) if row['knowledge_id'] == first]
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        assert candidate_rows(conn, [first, first, 'missing']) == expected
    finally:
        conn.set_trace_callback(None)
    selects = [sql for sql in statements if 'SELECT' in sql.upper()]
    assert len(selects) == 1
    assert 'ke.knowledge_id IN (' in selects[0]
    assert candidate_rows(conn, []) == []


@pytest.mark.parametrize('keys', [['x']*501, [''], [None], 'one'])
def test_invalid_candidate_requests_fail_before_reading(conn, keys):
    with pytest.raises(RetrievalError):
        candidate_rows(conn, keys)


def test_candidate_loader_does_not_return_revoked_content(conn):
    key = entry(conn)
    conn.execute("UPDATE knowledge_entries SET status='retired' WHERE knowledge_id=?", (key,))
    assert candidate_rows(conn, [key]) == []


def test_final_entry_loader_checks_acl_without_full_corpus_scan(conn, monkeypatch):
    from k3_support import knowledge_runtime as runtime
    key = entry(conn)
    def forbidden(*args, **kwargs):
        raise AssertionError('single-entry validation scanned the corpus')
    monkeypatch.setattr(runtime, 'corpus_rows', forbidden)
    result = runtime.load_approved_entry(conn, knowledge_id=key, requester_id=None, chat_id=None)
    assert result['knowledge_id'] == key
    conn.execute("UPDATE knowledge_entries SET disclosure_class='restricted' WHERE knowledge_id=?", (key,))
    assert runtime.load_approved_entry(conn, knowledge_id=key, requester_id=None, chat_id=None) is None
    conn.execute("UPDATE knowledge_entries SET status='retired' WHERE knowledge_id=?", (key,))
    assert runtime.load_approved_entry(conn, knowledge_id=key, requester_id=None, chat_id=None) is None
