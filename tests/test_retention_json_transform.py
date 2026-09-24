import json

import pytest

from k3_support.retention_json_fields import outbox_fields, redact_outbox_text


@pytest.mark.parametrize('action', ['reply', 'ack', 'clarify', 'send'])
def test_transform_removes_only_body_and_preserves_binding_metadata(action):
    payload = {'text':'私密正文', 'identity':'user', 'format':'markdown',
               'evidence_ids':['evidence-1'], 'reply_basis':'verified_evidence'}
    encoded = json.dumps(payload)
    args = dict(channel='feishu_im', action_type=action, payload_json=encoded)
    classified = outbox_fields(**args)
    result = redact_outbox_text(**args, expected_digest=classified['input_digest'])
    assert json.loads(result['payload_json']) == {**payload, 'text':''}
    assert result['removed_body_bytes'] == len(payload['text'].encode())
    assert not result['clear_allowed'] and '私密正文' not in str(result)
    assert json.loads(encoded) == payload
    with pytest.raises(ValueError, match='changed'):
        redact_outbox_text(**args, expected_digest='0'*64)


@pytest.mark.parametrize('payload', ['{}', '{"text":"a","text":"b","identity":"user"}',
                                    '{"text":"private","identity":"user","knowledge_release":{}}'])
def test_unknown_or_duplicate_fields_never_transform(payload):
    with pytest.raises(ValueError, match='unclassified'):
        redact_outbox_text(channel='feishu_im',action_type='reply',payload_json=payload,expected_digest='a'*64)
