"""Creation relations use observed target metadata and explicit read scopes."""

from copy import deepcopy

from .project_bug_query import item_id, validate_spaces
from .project_read_client import ProjectReadError

TYPES = {
    'work_item_related_select': 'workitem_related_select',
    'workitem_related_select': 'workitem_related_select',
    'work_item_related_multi_select': 'workitem_related_multi_select',
    'workitem_related_multi_select': 'workitem_related_multi_select',
}


def target(client, scope, field):
    command = 'workitem.meta-fields'
    response = client.read_page(command, {'project_key': scope['project_key'],
        'work_item_type': scope['type_key'], 'field_keys': [field['field_key']], 'page_num': 1})
    if not isinstance(response, dict) or response.get('host') != scope['host'] or response.get('command') != command:
        raise ProjectReadError('invalid_create_relation')
    payload = response.get('payload')
    if not isinstance(payload, dict) or not isinstance(payload.get('list'), list) or len(payload['list']) != 1:
        raise ProjectReadError('invalid_create_relation')
    row = payload['list'][0]
    if (payload.get('pagination') != {'page_num': 1, 'page_size': 50, 'has_more': False, 'total': 1}
            or not isinstance(row, dict) or row.get('field_key') != field['field_key']
            or row.get('field_type') != TYPES[field['field_type_key']]):
        raise ProjectReadError('invalid_create_relation')
    targets = row.get('related_work_item_info')
    if (not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict)
            or set(targets[0]) != {'project_key', 'work_item_type'}):
        raise ProjectReadError('ambiguous_create_relation_target')
    return targets[0]


def authorized(config, target_scope):
    scopes = validate_spaces(config.raw.get('project_integration', {}).get('search_spaces', []))
    matches = [s for s in scopes if s['project_key'] == target_scope['project_key']
               and s['type_key'] == target_scope['work_item_type']]
    if len(matches) != 1:
        raise PermissionError('related type needs an explicit configured read scope')
    return deepcopy(matches[0])


def validate(client, config, scope, metadata, values):
    """Return a reservation-time scope recheck after fresh ID-limited reads."""
    observed = []
    for field in metadata['FieldConfList']:
        kind, key = field.get('field_type_key'), field['field_key']
        if kind not in TYPES or key not in values or values[key] in (None, '', []):
            continue
        value = values[key]
        if TYPES[kind] == 'workitem_related_select':
            if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
                raise ValueError('related item must be an ID string: ' + key)
            ids = [int(value)]
        else:
            ids = value
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 20
                or any(not item_id(i) or i > 2**53-1 for i in ids) or len(set(ids)) != len(ids)):
            raise ValueError('invalid related item IDs: ' + key)
        bound = target(client, scope, field)
        read_scope = authorized(config, bound)
        if read_scope['allowed_item_ids'] is not None and not set(ids) <= set(read_scope['allowed_item_ids']):
            raise PermissionError('related item is outside configured read scope')
        result = client.query_bugs(read_scope | {'allowed_item_ids': ids})
        if (result.get('host') != scope['host'] or result.get('next_after_id') is not None
                or {r['item_id'] for r in result['items']} != {str(i) for i in ids}):
            raise ValueError('related items no longer resolve: ' + key)
        observed.append((bound, read_scope.copy()))

    def recheck():
        for bound, prior in observed:
            if authorized(config, bound) != prior:
                raise PermissionError('related read scope changed during preflight')
    return recheck


def search(conn, config, *, actor, grant_id, host, project_key, type_key, field_key, query,
           client_factory=None):
    from . import project_create_grants
    from .project_create_schema import required_fields
    from .project_read_client import MeegleReadClient
    from .project_refresh import _fingerprint, _selection

    if not isinstance(query, str) or not query.strip() or len(query) > 128:
        raise ValueError('provide a related item name')
    scope = {'host': host, 'project_key': project_key, 'type_key': type_key}

    def guard():
        if not project_create_grants.covers(conn, grant_id=grant_id, actor=actor, **scope):
            raise PermissionError('related lookup is outside current creation grant')
        return _selection(conn, config, {'host': host}, actor)

    reader = guard()
    stamp = _fingerprint(conn, config, reader)
    client = (client_factory or (lambda r: MeegleReadClient(**{k: v for k, v in r.items() if k != 'enabled'})))(reader)
    command = 'workitem.meta-create-fields'
    response = client.read_page(command, {'project_key': project_key, 'work_item_type': type_key})
    if not isinstance(response, dict) or response.get('host') != host or response.get('command') != command:
        raise ProjectReadError('invalid_create_metadata')
    metadata = response.get('payload')
    required_fields(metadata)
    fields = [f for f in metadata['FieldConfList'] if f['field_key'] == field_key and f.get('field_type_key') in TYPES]
    if len(fields) != 1:
        raise ValueError('field is not a current related-item field')
    guard()
    bound = target(client, scope, fields[0])
    read_scope = authorized(config, bound)
    guard()
    result = client.query_bugs(read_scope, keyword=query.strip())
    if (_fingerprint(conn, config, guard()) != stamp or authorized(config, bound) != read_scope):
        raise PermissionError('related lookup authority changed while reading')
    if result.get('host') != host:
        raise ProjectReadError('invalid_query_response')
    return {'options': [{'value': r['item_id'], 'label': r['title']} for r in result['items']],
            'narrow_query': result.get('next_after_id') is not None}
