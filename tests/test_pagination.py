import pytest

from k3_support.pagination import PaginationError, decode_page


@pytest.mark.parametrize('change', [
    {'messages': None, 'has_more': False},
    {'messages': [None], 'has_more': False},
    {'messages': [{'message_id': 123}], 'has_more': False},
    {'messages': [{'message_id': ' '}], 'has_more': False},
    {'messages': []},
    {'messages': [], 'has_more': 'false'},
    {'messages': [], 'has_more': 0},
    {'messages': [], 'has_more': False, 'page_token': 3},
    {'messages': [], 'has_more': False, 'page_token': 'next'},
    {'messages': [], 'has_more': True},
    {'messages': [], 'has_more': True, 'page_token': 'a', 'next_page_token': 'b'},
])
def test_invalid_page_is_never_coerced_to_completion(change):
    with pytest.raises(PaginationError):
        decode_page(change, allow_empty_more=True)


def test_metadata_only_and_consistent_dual_completion_are_supported():
    meta = {'pagination': {'complete': False, 'next_token': 'opaque'}}
    data = {'messages': [{'message_id': 'mail-1'}]}
    page = decode_page(data, meta=meta)
    assert page.has_more and page.next_token == 'opaque'
    assert decode_page({**data, 'has_more': True, 'page_token': 'opaque'}, meta=meta) == page
    assert not decode_page({'messages': []}, meta={'pagination': {'complete': True}}).has_more


@pytest.mark.parametrize('meta', [
    {'pagination': {'complete': False}},
    {'pagination': {'complete': 'true'}},
    {'pagination': {'next_token': 'unexpected'}},
    {'pagination': []},
])
def test_conflicting_or_malformed_outer_envelope_is_rejected(meta):
    with pytest.raises(PaginationError):
        decode_page({'messages': [], 'has_more': False}, meta=meta)


def test_empty_continuation_is_explicit_policy_not_completion():
    data = {'messages': [], 'has_more': True, 'page_token': 'opaque'}
    with pytest.raises(PaginationError):
        decode_page(data)
    assert decode_page(data, allow_empty_more=True).has_more
    with pytest.raises(PaginationError, match='repeated'):
        decode_page(data, allow_empty_more=True, current_token='opaque')
