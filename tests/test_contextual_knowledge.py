"""Context-bound retrieval checks use synthetic messages, never external data."""
from __future__ import annotations

import pytest
from test_knowledge_runtime import choose_first, professional

from k3_support.conversation_context import (
    admit_im_event,
    project_context,
    resolve_event_context,
)
from k3_support.knowledge_corpus import build
from k3_support.knowledge_runtime import (
    RetrievalError,
    load_approved_entry,
    query_knowledge,
)


def message(conn, config, number, content, *, parent=None):
    payload = {'content': content, 'chat_type': 'p2p'}
    if parent:
        payload['parent_id'] = parent
    event_pk, _ = admit_im_event(conn, config, {
        'source': 'feishu_user_poll', 'identity': 'user', 'external_id': f'om_scope_{number}',
        'chat_id': 'oc_peer', 'sender_id': 'ou_peer', 'thread_id': None,
        'payload': payload, 'occurred_at': f'2026-09-07T0{number}:00:00+00:00',
    })
    return project_context(conn, resolve_event_context(conn, event_pk)['context_id'])


def ask_context(conn, context, **kwargs):
    # Prepare this synthetic corpus explicitly. Query-time rebuilding would
    # hide the stale-generation protection tested by the production entry.
    assert build(conn)['built']
    return query_knowledge(conn, query=context['query'], requester_id='ou_peer', chat_id='oc_peer',
                           context_binding=context['binding'], selector=choose_first, **kwargs)


def test_current_correction_not_concatenated_old_pico_mentions_controls_eligibility(conn, config):
    key = professional(conn, kind='document_route')
    first = message(conn, config, 1, 'Pico风扇如何配置')
    initial = ask_context(conn, first)
    assert initial['selected_knowledge_id'] == key
    assert initial['selected_entry']['knowledge_context_binding'] == first['binding']
    second = message(conn, config, 2, '更正，不是Pico，是EVB', parent='om_scope_1')
    result = ask_context(conn, second)
    assert result['scope']['board'] == 'evb'
    assert result['selected_entry'] is None
    assert load_approved_entry(conn, knowledge_id=key, requester_id='ou_peer', chat_id='oc_peer',
                               query=second['query'], context_binding=second['binding']) is None


def test_old_binding_and_caller_replacement_query_or_scope_cannot_impersonate_context(conn, config):
    professional(conn, kind='document_route')
    first = message(conn, config, 1, 'Pico风扇')
    with pytest.raises(RetrievalError, match='context'):
        query_knowledge(conn, query='假的替代问题', requester_id='ou_peer', chat_id='oc_peer', context_binding=first['binding'])
    with pytest.raises(RetrievalError, match='context'):
        ask_context(conn, first, observed_scope={'board': 'evb'})
    message(conn, config, 2, '不是Pico，是EVB', parent='om_scope_1')
    with pytest.raises(RetrievalError, match='context'):
        ask_context(conn, first)


def test_new_message_during_model_selection_invalidates_old_selected_answer(conn, config):
    professional(conn, kind='document_route')
    assert build(conn)['built']
    first = message(conn, config, 1, 'Pico风扇')

    def select(query, catalog):
        message(conn, config, 2, '不是Pico，是EVB', parent='om_scope_1')
        return choose_first(query, catalog)

    with pytest.raises(RetrievalError, match='context'):
        query_knowledge(conn, query=first['query'], requester_id='ou_peer', chat_id='oc_peer',
                        context_binding=first['binding'], selector=select)
