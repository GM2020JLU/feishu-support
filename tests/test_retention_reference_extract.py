import pytest

from k3_support.retention_reference_extract import (
    ExtractionIncomplete, extract_json, reference_digest,
)


def test_duplicate_keys_map_keys_and_future_targets_are_preserved():
    result = extract_json('{"future-id":"old-id","future-id":"new-id","map":{"target":0}}')
    assert result == frozenset(map(reference_digest, ['future-id', 'old-id', 'new-id', 'map', 'target']))


def test_nested_arrays_null_and_scalar_values():
    assert extract_json(None) == frozenset()
    assert extract_json('null') == frozenset()
    assert extract_json('[true,1,["ref",{"key":"ref"}]]') == frozenset(map(reference_digest, ['key', 'ref']))
    assert extract_json('"scalar"') == frozenset([reference_digest('scalar')])


@pytest.mark.parametrize('value', ['', '{', b'{}', 1, 'NaN', '{"x":Infinity}'])
def test_invalid_sources_never_return_empty_success(value):
    with pytest.raises(ExtractionIncomplete):
        extract_json(value)


def test_budget_exhaustion_never_returns_partial_references():
    with pytest.raises(ExtractionIncomplete, match='byte budget'):
        extract_json('"中文"', max_bytes=5)
    with pytest.raises(ExtractionIncomplete, match='traversal budget'):
        extract_json('["a","b"]', max_nodes=2)


def test_collisions_overprotect_instead_of_dropping_references(monkeypatch):
    from k3_support import retention_reference_extract as module
    monkeypatch.setattr(module, 'reference_digest', lambda value: 'collision')
    assert extract_json('["a","b"]') == frozenset(['collision'])
    assert module.reference_digest('future-target') in extract_json('["a"]')


def test_escaped_surrogate_does_not_hide_other_references():
    assert reference_digest('target') in extract_json('["\\ud800","target"]')
