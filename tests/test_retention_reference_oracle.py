import pytest

from k3_support.retention_reference_extract import extract_json, reference_digest
from k3_support.retention_reference_oracle import OracleIncomplete, json_reference_digests


@pytest.mark.parametrize('value', [None, 'null', '42', 'true', '"scalar"',
    '{"duplicate":"first","duplicate":"second"}',
    '{"future":{"nested":["target",3,{"map-key":"value"}]}}',
    '["中文","emoji 😀","quote\\\"", "line\\n"]', '["before\\u0000after"]'])
def test_independent_sqlite_oracle_matches_reference_semantics(value):
    assert json_reference_digests(value) == extract_json(value)


def test_oracle_does_not_call_production_extractor(monkeypatch):
    from k3_support import retention_reference_extract
    monkeypatch.setattr(retention_reference_extract, 'extract_json', lambda value: frozenset())
    assert json_reference_digests('{"key":"value"}') == {reference_digest('key'), reference_digest('value')}


@pytest.mark.parametrize('value', ['{', 'NaN', b'{}'])
def test_oracle_invalid_input_cannot_be_empty_success(value):
    with pytest.raises(OracleIncomplete):
        json_reference_digests(value)


def test_oracle_limits_fail_without_partial_result():
    with pytest.raises(OracleIncomplete, match='byte budget'):
        json_reference_digests('"中文"', max_bytes=5)
    with pytest.raises(OracleIncomplete, match='node budget'):
        json_reference_digests('["first","second"]', max_nodes=2)
