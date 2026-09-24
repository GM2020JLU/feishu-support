"""Current official creation requirements, independent of browser declarations.

The observed meta-create-fields contract uses FieldConfList/is_required=1.
Default display policies and conditional validity are not transport defaults;
never synthesize values from them. This check is necessary, not proof that all
server-side business validation or permission checks will pass.
"""

from .project_read_client import ProjectReadError


def empty(value):
    return value is None or (isinstance(value, str) and not value.strip()) or (
        isinstance(value, (list, dict)) and not value
    )


def required_fields(payload):
    if not isinstance(payload, dict):
        raise ProjectReadError("invalid_create_metadata")
    rows = payload.get("FieldConfList")
    if not isinstance(rows, list) or not rows or len(rows) > 200:
        raise ProjectReadError("invalid_create_metadata")
    seen, required = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ProjectReadError("invalid_create_metadata")
        key, flag = row.get("field_key"), row.get("is_required")
        if (not isinstance(key, str) or not key.strip() or len(key) > 256
                or key in seen or type(flag) is not int or flag not in {0, 1}):
            raise ProjectReadError("invalid_create_metadata")
        seen.add(key)
        if flag == 1:
            required.append(key)
    return required


def check(client, scope, values):
    command = "workitem.meta-create-fields"
    response = client.read_page(command, {
        "project_key": scope["project_key"], "work_item_type": scope["type_key"],
    })
    if (not isinstance(response, dict) or response.get("host") != scope["host"]
            or response.get("command") != command):
        raise ProjectReadError("invalid_create_metadata")
    missing = [key for key in required_fields(response.get("payload"))
               if key not in values or empty(values[key])]
    if missing:
        raise ValueError("required Project fields are missing or empty: " + ", ".join(missing))
    from .project_create_users import validate
    validate(client, scope, response["payload"], values)
    options = select_options(client, scope, response['payload'])
    for key, choices in options.items():
        if key in values and not empty(values[key]) and (
                not isinstance(values[key], str) or values[key] not in {o['value'] for o in choices}):
            raise ValueError('invalid current Project option for field: ' + key)
    return response['payload']


def form(conn, config, *, actor, grant_id, host, project_key, type_key, client_factory=None):
    """Read labels/types for exactly the operator's active creation scope."""
    from . import project_create_grants
    from .project_read_client import MeegleReadClient
    from .project_refresh import _fingerprint, _selection

    def guard():
        if not project_create_grants.covers(conn, grant_id=grant_id, actor=actor,
                                           host=host, project_key=project_key, type_key=type_key):
            raise PermissionError('creation metadata is outside current grant')
        return _selection(conn, config, {'host': host}, actor)

    reader = guard()
    stamp = _fingerprint(conn, config, reader)
    client = (client_factory or (lambda r: MeegleReadClient(**{k:v for k,v in r.items() if k!='enabled'})))(reader)
    command = 'workitem.meta-create-fields'
    response = client.read_page(command, {'project_key': project_key, 'work_item_type': type_key})
    if not isinstance(response, dict) or response.get('host') != host or response.get('command') != command:
        raise ProjectReadError('invalid_create_metadata')
    payload = response.get('payload')
    required = required_fields(payload)
    options = select_options(client, {"host":host,"project_key":project_key,"type_key":type_key}, payload, guard=guard)
    from .project_create_related import TYPES as RELATED_TYPES
    fields = []
    for row in payload['FieldConfList']:
        label, kind = row.get('field_name'), row.get('field_type_key')
        if (not isinstance(label, str) or not label.strip() or len(label)>1024
                or not isinstance(kind, str) or not kind.strip() or len(kind)>128):
            raise ProjectReadError('invalid_create_metadata')
        fields.append({'field_key': row['field_key'], 'label': label, 'type': kind,
                       'required': row['field_key'] in required,
                       'editor': 'select' if row['field_key'] in options else 'related' if kind in RELATED_TYPES else 'user' if kind in {'user','multi_user','multi-user'} else 'text' if kind in {'text', 'multi_text'} else 'json',
                       **({'options':options[row['field_key']]} if row['field_key'] in options else {})})
    if _fingerprint(conn, config, guard()) != stamp:
        raise PermissionError('creation metadata authority changed while reading')
    return {'scope': {'host':host, 'project_key':project_key, 'type_key':type_key},
            'fields': fields, 'defaults_applied': False, 'options_validated': False}


def select_options(client, scope, payload, *, guard=None):
    """Observed flat/tree single-select contracts; never infer member values."""
    types = {row['field_key']: {'select':'select', 'tree_select':'tree-select', 'tree-select':'tree-select'}[row['field_type_key']]
             for row in payload['FieldConfList'] if row.get('field_type_key') in {'select','tree_select','tree-select'}}
    keys = list(types)
    if not keys:
        return {}
    result, total = {}, None
    for page in range(1, 5):
        if guard is not None:
            guard()
        envelope = client.read_page('workitem.meta-fields', {
            'project_key': scope['project_key'], 'work_item_type': scope['type_key'],
            'field_keys': keys, 'page_num': page,
        })
        if (not isinstance(envelope, dict) or envelope.get('host') != scope['host']
                or envelope.get('command') != 'workitem.meta-fields'):
            raise ProjectReadError('invalid_create_options')
        data = envelope.get('payload')
        if not isinstance(data, dict):
            raise ProjectReadError('invalid_create_options')
        rows, pagination = data.get('list'), data.get('pagination')
        if (not isinstance(rows, list) or len(rows)>50 or not isinstance(pagination, dict)
                or type(pagination.get('has_more')) is not bool
                or type(pagination.get('page_num')) is not int or pagination['page_num'] != page
                or pagination.get('page_size') != 50
                or type(pagination.get('total')) is not int or pagination['total'] != len(keys)):
            raise ProjectReadError('invalid_create_options')
        if total is not None and pagination['total'] != total:
            raise ProjectReadError('invalid_create_options')
        total = pagination['total']
        for row in rows:
            if (not isinstance(row, dict) or row.get('field_key') not in keys
                    or row['field_key'] in result or row.get('field_type') != types[row['field_key']]):
                raise ProjectReadError('invalid_create_options')
            result[row['field_key']] = _choices(row.get('option'), tree=types[row['field_key']]=='tree-select')
        if not pagination['has_more']:
            if set(result) != set(keys):
                raise ProjectReadError('invalid_create_options')
            return result
    raise ProjectReadError('create_options_page_limit')


def _choices(options, *, tree):
    if not isinstance(options, list):
        raise ProjectReadError('invalid_create_options')
    pending = [(option, (), 0) for option in reversed(options)]
    normalized, seen = [], set()
    while pending:
        option, ancestors, depth = pending.pop()
        allowed = {'option_id','option_name'} | ({'children'} if tree else set())
        if (not isinstance(option, dict) or not {'option_id','option_name'} <= option.keys()
                or set(option)-allowed or depth>8 or len(seen)>=1000):
            raise ProjectReadError('invalid_create_options')
        key, label = option['option_id'], option['option_name']
        if (not isinstance(key,str) or not key or len(key)>256 or key in seen
                or not isinstance(label,str) or not label or len(label)>1024):
            raise ProjectReadError('invalid_create_options')
        seen.add(key)
        children = option.get('children', [])
        if not isinstance(children, list):
            raise ProjectReadError('invalid_create_options')
        if children:
            pending.extend((child, ancestors+(label,), depth+1) for child in reversed(children))
        else:
            normalized.append({'value':key, 'label':' / '.join((*ancestors,label))})
    return normalized
