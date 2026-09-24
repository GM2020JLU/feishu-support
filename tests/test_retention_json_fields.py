import json

import pytest

from k3_support.retention_json_fields import outbox_fields


def classify(value):
    return outbox_fields(channel='feishu_im', action_type='reply', payload_json=json.dumps(value))


def test_known_text_is_separated_from_identity_and_evidence_without_disclosure():
    value = {'text':'private 原文', 'identity':'user', 'format':'markdown',
             'evidence_ids':['evd-fixture'], 'reply_basis':'verified_evidence'}
    result = classify(value)
    assert result['shape_recognized'] and not result['clear_allowed']
    assert result['body_paths'] == ['/text']
    assert result['body_bytes'] == len(value['text'].encode())
    assert '/identity' in result['retained_paths'] and '/evidence_ids' in result['retained_paths']
    assert value['text'] not in str(result) and 'evd-fixture' not in str(result)


@pytest.mark.parametrize('extra', [{'unknown':'private'}, {'knowledge_release':{}},
                                  {'text':{}}, {'evidence_ids':'not-a-list'}, {'identity':'other'}])
def test_unknown_shape_is_not_partially_cleared(extra):
    result = classify({'text':'private', 'identity':'user', **extra})
    assert not result['shape_recognized'] and not result['clear_allowed']
    assert result['body_paths'] == []


def test_duplicate_keys_are_rejected():
    result = outbox_fields(channel='feishu_im', action_type='reply',
                          payload_json='{"text":"first","text":"second","identity":"user"}')
    assert result['reason'] == 'invalid_json'


@pytest.mark.parametrize('field', ['identity', 'format', 'reply_basis'])
@pytest.mark.parametrize('value', [{}, [], None, 123])
def test_malformed_field_types_fail_closed_without_raising(field, value):
    result = classify({'text':'body', 'identity':'user', field:value})
    assert not result['shape_recognized']


def test_escaped_invalid_unicode_is_not_accepted_as_body():
    result = classify({'text':'\ud800', 'identity':'user'})
    assert result['reason'] == 'invalid_json'


@pytest.mark.parametrize('release', [None, {}, {'provenance': {'knowledge_scope_facts':
    {'mentions': [{'text': 'PRIVATE SOURCE FRAGMENT', 'value': 'PRIVATE VALUE'}]}}}])
def test_release_provenance_is_an_explicit_content_hold(release):
    from k3_support.retention_json_fields import redact_outbox_text
    value = {'text':'PRIVATE REPLY','identity':'user','knowledge_release':release}
    encoded = json.dumps(value)
    result = classify(value)
    assert result['reason'] == 'release_provenance_requires_content_review'
    assert result['release_provenance_review_required'] is True
    assert not result['shape_recognized'] and not result['clear_allowed']
    assert 'PRIVATE' not in str(result)
    with pytest.raises(ValueError,match='unclassified'):
        redact_outbox_text(channel='feishu_im',action_type='reply',payload_json=encoded,
                           expected_digest='a'*64)


@pytest.mark.parametrize('fragment', ['NaN','Infinity','-Infinity',
    '{"text":"first","text":"second"}', '[' * 1100 + '0' + ']' * 1100],
    ids=['nan','infinity','negative-infinity','duplicate','deep'])
def test_invalid_nested_release_json_is_rejected_before_classification(fragment):
    from k3_support.retention_json_fields import redact_outbox_text
    encoded = '{"text":"PRIVATE","identity":"user","knowledge_release":{"provenance":' + fragment + '}}'
    result = outbox_fields(channel='feishu_im',action_type='reply',payload_json=encoded)
    assert result['reason'] == 'invalid_json'
    assert not result['shape_recognized'] and not result['clear_allowed']
    assert 'PRIVATE' not in str(result)
    with pytest.raises(ValueError,match='unclassified'):
        redact_outbox_text(channel='feishu_im',action_type='reply',payload_json=encoded,
                           expected_digest='a'*64)


@pytest.mark.parametrize('count,recognized', [(32764,True),(32765,False)])
def test_node_limit_has_an_explicit_boundary(count, recognized):
    result = classify({'text':'a','identity':'user','evidence_ids':[''] * count})
    assert result['shape_recognized'] is recognized
    if not recognized:
        assert result['reason'] == 'invalid_json'


@pytest.mark.parametrize('depth,reason', [(61,'release_provenance_requires_content_review'),
                                        (62,'invalid_json')])
def test_depth_limit_has_an_explicit_boundary(depth, reason):
    encoded = '{"knowledge_release":{"provenance":' + '[' * depth + '0' + ']' * depth + '}}'
    result = outbox_fields(channel='feishu_im',action_type='reply',payload_json=encoded)
    assert result['reason'] == reason and not result['clear_allowed']
