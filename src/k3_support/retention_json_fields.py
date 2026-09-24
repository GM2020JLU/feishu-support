"""Explicit JSON field semantics; unknown shapes never acquire clear permission."""
import json

from .ids import digest, canonical_json

POLICY = 'outbox-text-fields-v1'


def outbox_fields(*, channel, action_type, payload_json):
    base = {'policy': POLICY, 'shape_recognized': False, 'clear_allowed': False,
            'body_bytes': 0, 'body_paths': [], 'retained_paths': []}
    if not isinstance(payload_json, str):
        return {**base, 'reason': 'input_limit_or_type'}
    try:
        if len(payload_json.encode()) > 262144:
            return {**base, 'reason': 'input_limit_or_type'}
    except UnicodeError:
        return {**base, 'reason': 'invalid_json'}
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    def reject_constant(value):
        raise ValueError('non-finite JSON constant')
    try:
        value = json.loads(payload_json, object_pairs_hook=unique, parse_constant=reject_constant)
        pending, visited = [(value, 1)], 0
        while pending:
            node, depth = pending.pop()
            visited += 1
            if depth > 64 or visited > 32768:
                raise ValueError('JSON structural limit exceeded')
            if isinstance(node, dict):
                pending.extend((child, depth+1) for child in node.values())
            elif isinstance(node, list):
                pending.extend((child, depth+1) for child in node)
    except (ValueError, TypeError, RecursionError):
        return {**base, 'reason': 'invalid_json'}
    if channel != 'feishu_im' or action_type not in ('reply', 'ack', 'clarify', 'send'):
        return {**base, 'reason': 'unsupported_transport_shape'}
    if isinstance(value, dict) and 'knowledge_release' in value:
        # Runtime provenance includes knowledge_scope_facts, not just hashes:
        # its source-bound mentions can contain private message fragments. Do
        # not label this opaque structure metadata or clear only /text while
        # implying that all reply content has been accounted for.
        return {**base, 'reason': 'release_provenance_requires_content_review',
                'release_provenance_review_required': True}
    allowed = {'text', 'identity', 'format', 'evidence_ids', 'reply_basis'}
    if (not isinstance(value, dict) or set(value)-allowed or not isinstance(value.get('text'), str)
            or value.get('identity') not in ('user', 'bot')
            or value.get('format', 'text') not in ('text', 'markdown')
            or value.get('reply_basis', 'verified_evidence') not in ('verified_evidence', 'verified_link_route')
            or not isinstance(value.get('evidence_ids', []), list)
            or any(not isinstance(item, str) for item in value.get('evidence_ids', []))):
        return {**base, 'reason': 'unknown_or_invalid_fields'}
    try:
        body_bytes = len(value['text'].encode())
        binding = digest({'policy': POLICY, 'channel': channel,
                          'action_type': action_type, 'payload': value})
    except UnicodeError:
        return {**base, 'reason': 'invalid_json'}
    return {**base, 'shape_recognized': True, 'reason': 'classified_not_authorized',
            'body_paths': ['/text'], 'body_bytes': body_bytes,
            'retained_paths': ['/'+key for key in sorted(set(value)-{'text'})],
            'input_digest': binding}


def redact_outbox_text(*, channel, action_type, payload_json, expected_digest):
    """Pure candidate transform; caller still owes all transactional safety gates.

    Does not write, authorize deletion, change delivery state or make a reply
    sendable. Keep identity and evidence metadata byte-value equivalent.
    """
    classified = outbox_fields(channel=channel, action_type=action_type, payload_json=payload_json)
    if not classified['shape_recognized']:
        raise ValueError('unclassified Outbox payload cannot be transformed')
    if not isinstance(expected_digest, str) or expected_digest != classified['input_digest']:
        raise ValueError('Outbox payload changed since classification')
    value = json.loads(payload_json)
    value['text'] = ''
    output = canonical_json(value)
    return {'payload_json': output, 'input_digest': expected_digest,
            'output_digest': digest(value), 'removed_body_bytes': classified['body_bytes'],
            'policy': POLICY, 'clear_allowed': False, 'scope': 'pure_candidate_transform_only'}
