import pytest
from test_knowledge_runtime import entry

from k3_support.knowledge_corpus import build
from k3_support.knowledge_lexical import recall


def test_fts_pages_past_inapplicable_results_without_loading_bodies(conn):
    for n in range(85):
        entry(conn, ('boot failure '*10)+str(n), version='wrong')
    target = entry(conn, 'boot failure correct', version='right')
    build(conn)
    result = recall(conn, query='boot failure', scope={'software_version': 'right'}, limit=1)
    assert result['complete']
    assert [row['knowledge_id'] for row in result['items']] == [target]
    assert result['metadata_scanned'] > 80
    assert all('answer_markdown' not in row for row in result['items'])


def test_budget_exhaustion_is_not_reported_as_no_answer(conn):
    for n in range(5):
        entry(conn, f'boot failure {n}', version='wrong')
    build(conn)
    result = recall(conn, query='boot', scope={'software_version': 'right'}, limit=1, metadata_budget=2)
    assert not result['items']
    assert not result['complete'] and result['reason'] == 'metadata_budget_exhausted'


def test_acl_is_filtered_before_candidate_budget(conn):
    for n in range(5):
        entry(conn, f'boot private {n}', disclosure='restricted')
    target = entry(conn, 'boot public')
    build(conn)
    result = recall(conn, query='boot', limit=1, metadata_budget=1)
    assert [row['knowledge_id'] for row in result['items']] == [target]


@pytest.mark.parametrize('query', ['u-boot', 'env set bootargs "x"', '风扇怎么调', 'nothing-matching'])
def test_fts_special_queries_do_not_fallback_to_corpus(conn, query):
    entry(conn)
    build(conn)
    result = recall(conn, query=query)
    assert result['complete']


def test_chinese_bigram_inside_long_fts_token_is_recalled(conn):
    target = entry(conn, '调节风扇转速说明')
    build(conn)
    result = recall(conn, query='风扇怎么调')
    assert target in [row['knowledge_id'] for row in result['items']]
