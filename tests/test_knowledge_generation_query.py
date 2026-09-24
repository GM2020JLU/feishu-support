from test_knowledge_runtime import entry, choose_first

from k3_support import knowledge_runtime as runtime
from k3_support.knowledge_corpus import build


def test_unbuilt_generation_abstains_without_model_or_full_scan(conn, monkeypatch):
    entry(conn)
    def forbidden(*args, **kwargs):
        raise AssertionError('unbuilt query must not scan or call model')
    monkeypatch.setattr(runtime, 'corpus_rows', forbidden)
    result = runtime.query_knowledge(conn, query='风扇', requester_id=None, chat_id=None, selector=forbidden)
    assert result['abstention_reason'] == 'corpus_not_current'
    assert result['selected_entry'] is None


def test_built_sqlite_query_uses_candidate_bodies_and_complete_digest(conn, monkeypatch):
    key = entry(conn, '风扇调节')
    entry(conn, 'unrelated storage')
    built = build(conn)
    def forbidden(*args, **kwargs):
        raise AssertionError('query scanned complete corpus')
    monkeypatch.setattr(runtime, 'corpus_rows', forbidden)
    result = runtime.query_knowledge(conn, query='风扇', requester_id=None, chat_id=None, selector=choose_first)
    assert result['selected_knowledge_id'] == key
    assert result['runtime_binding']['corpus_digest'] == built['corpus_digest']


def test_model_time_corpus_change_blocks_selection(conn):
    key = entry(conn, '风扇调节')
    build(conn)
    def selector(query, catalog):
        selected = choose_first(query, catalog)
        conn.execute("UPDATE knowledge_entries SET disclosure_class='restricted' WHERE knowledge_id=?", (key,))
        return selected
    result = runtime.query_knowledge(conn, query='风扇', requester_id=None, chat_id=None, selector=selector)
    assert result['selected_entry'] is None
    assert result['abstention_reason'] == 'corpus_not_current'


def test_source_only_change_changes_runtime_binding_after_explicit_rebuild(conn):
    entry(conn, '风扇调节')
    conn.execute("INSERT INTO source_registry(source_id,source_type,stable_external_id) VALUES('s','doc','d')")
    build(conn)
    first = runtime.query_knowledge(conn, query='风扇', requester_id=None, chat_id=None)
    conn.execute("UPDATE source_registry SET source_version='v2' WHERE source_id='s'")
    build(conn)
    second = runtime.query_knowledge(conn, query='风扇', requester_id=None, chat_id=None)
    assert first['runtime_binding']['corpus_digest'] == second['runtime_binding']['corpus_digest']
    assert first['runtime_binding']['sources_digest'] != second['runtime_binding']['sources_digest']
    assert first['runtime_binding']['corpus_generation'] != second['runtime_binding']['corpus_generation']
