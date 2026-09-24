"""Named, space-scoped member lookup for creation, never a directory dump."""

from .project_read_client import ProjectReadError

USER_TYPES = {'user', 'multi_user', 'multi-user'}


def lookup(client, scope, keys):
    result = client.read_page('user.search', {'project_key': scope['project_key'], 'user_keys': keys})
    if (not isinstance(result, dict) or result.get('host') != scope['host']
            or result.get('command') != 'user.search'):
        raise ProjectReadError('invalid_create_users')
    rows = result.get('payload')
    if not isinstance(rows, list) or len(rows) > 200:
        raise ProjectReadError('invalid_create_users')
    choices, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get('status') != 'activated':
            raise ProjectReadError('invalid_create_users')
        key = row.get('user_key')
        label = row.get('name_cn') or row.get('name_en') or row.get('username')
        if (not isinstance(key, str) or not key.strip() or len(key) > 256 or key in seen
                or not isinstance(label, str) or not label.strip() or len(label) > 1024):
            raise ProjectReadError('invalid_create_users')
        seen.add(key)
        choices.append({'value': key, 'label': label})
    return choices


def validate(client, scope, metadata, values):
    """Exact submitted IDs must still resolve; names and stale users are rejected."""
    for field in metadata['FieldConfList']:
        kind, key = field.get('field_type_key'), field['field_key']
        if kind not in USER_TYPES or key not in values or values[key] in (None, '', []):
            continue
        value = values[key]
        keys = [value] if kind == 'user' else value
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 20
                or any(not isinstance(k, str) or not k.strip() or len(k) > 256 for k in keys)
                or len(set(keys)) != len(keys)):
            raise ValueError('invalid Project member value for field: ' + key)
        if {choice['value'] for choice in lookup(client, scope, keys)} != set(keys):
            raise ValueError('Project members no longer resolve for field: ' + key)


def search(conn, config, *, actor, grant_id, host, project_key, type_key, field_key, query,
           client_factory=None):
    from . import project_create_grants
    from .project_create_schema import required_fields
    from .project_read_client import MeegleReadClient
    from .project_refresh import _fingerprint, _selection

    if not isinstance(query, str) or not query.strip() or len(query) > 128:
        raise ValueError('provide one member name or identifier')
    scope = {'host': host, 'project_key': project_key, 'type_key': type_key}

    def guard():
        if not project_create_grants.covers(conn, grant_id=grant_id, actor=actor, **scope):
            raise PermissionError('member lookup is outside current creation grant')
        return _selection(conn, config, {'host': host}, actor)

    reader = guard()
    stamp = _fingerprint(conn, config, reader)
    client = (client_factory or (lambda r: MeegleReadClient(**{k: v for k, v in r.items() if k != 'enabled'})))(reader)
    command = 'workitem.meta-create-fields'
    result = client.read_page(command, {'project_key': project_key, 'work_item_type': type_key})
    if not isinstance(result, dict) or result.get('host') != host or result.get('command') != command:
        raise ProjectReadError('invalid_create_metadata')
    metadata = result.get('payload')
    required_fields(metadata)
    if not any(row['field_key'] == field_key and row.get('field_type_key') in USER_TYPES
               for row in metadata['FieldConfList']):
        raise ValueError('field is not a current creation member field')
    guard()
    choices = lookup(client, scope, [query.strip()])
    if _fingerprint(conn, config, guard()) != stamp:
        raise PermissionError('member lookup authority changed while reading')
    return {'options': choices}
