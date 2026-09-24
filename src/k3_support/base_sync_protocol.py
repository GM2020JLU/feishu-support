"""Durable per-remote-entity serialization; transport runs without a DB writer lock.

An operation ID owns one immutable generation. A dispatched/unknown operation
never expires: a lease is not evidence that a remote mutation has stopped.
"""

import json
from datetime import UTC, datetime, timedelta

from .base_sync_attempt import AttemptRef
from .db import transaction
from .ids import canonical_json, digest, new_id
from .timeutil import iso_now


class BaseSyncError(RuntimeError):
    pass


class BaseSyncBusy(BaseSyncError):
    pass


class BaseSyncSuperseded(BaseSyncError):
    pass


class BaseSyncInputChanged(BaseSyncError):
    pass


def job_input_digest(*, entity_type, entity_id, mirror, base_digest, table_id):
    return digest({'entity_type': entity_type, 'entity_id': entity_id, 'mirror': mirror,
                   'base_digest': base_digest, 'table_id': table_id})


def _legacy_hold(conn, entity_type, entity_id):
    return conn.execute('''SELECT 1 FROM base_sync_legacy_holds WHERE state='unverified'
        AND (entity_type IS NULL OR entity_id IS NULL OR entity_id=''
             OR entity_type NOT IN ('case','knowledge','mail','health')
             OR (entity_type=? AND entity_id=?)) LIMIT 1''', (entity_type, entity_id)).fetchone() is not None


def _ref(row):
    if row['job_id'] is None:
        return None
    return AttemptRef(row['job_id'], row['attempt_no'], row['lease_owner'], row['input_digest'], row['lifecycle_round'])


def _owns(conn, op, *, expected):
    row = conn.execute('SELECT * FROM base_sync_operations WHERE operation_id=?', (op,)).fetchone()
    if row is None or row['state'] != expected:
        raise BaseSyncSuperseded('Base operation generation is no longer owned')
    ref = _ref(row)
    if ref is not None and ref.current(conn) is None:
        raise BaseSyncSuperseded('Base attempt is no longer owned')
    if ref is None and datetime.fromisoformat(row['expires_at']) <= datetime.now(UTC):
        raise BaseSyncSuperseded('Base direct operation expired before dispatch')
    return row


def _enabled(conn, config):
    from .runtime_control import capability_allowed
    if config.mode != 'active' or not config.feature('base_sync') or not capability_allowed(conn, config, 'base_sync'):
        raise BaseSyncError('Base sync is disabled')


def _reserve(conn, config, entity_type, entity_id, ref):
    from .base_sync import ENTITY_TABLE_KEYS
    if entity_type not in ENTITY_TABLE_KEYS:
        raise BaseSyncError('unsupported Base entity type')
    base_digest = digest(config.raw['base']['app_token'])
    table = str(config.raw['base'][ENTITY_TABLE_KEYS[entity_type]])
    op = new_id('bso')
    stamp = iso_now()
    with transaction(conn):
        _enabled(conn, config)
        if ref is not None and ref.current(conn) is None:
            raise BaseSyncSuperseded('Base attempt is no longer owned')
        if _legacy_hold(conn, entity_type, entity_id):
            raise BaseSyncBusy('Historical Base requests require migration inventory before sending')
        previous = conn.execute('''SELECT * FROM base_sync_operations
            WHERE base_digest=? AND table_id=? AND entity_type=? AND entity_id=?
              AND state IN ('reserved','prepared','dispatched','unknown')''',
            (base_digest, table, entity_type, entity_id)).fetchone()
        if previous is not None:
            old_ref = _ref(previous)
            obsolete = (old_ref.current(conn) is None if old_ref is not None
                        else datetime.fromisoformat(previous['expires_at']) <= datetime.now(UTC))
            if previous['state'] in ('reserved', 'prepared') and obsolete:
                conn.execute("UPDATE base_sync_operations SET state='cancelled',updated_at=? WHERE operation_id=?", (stamp, previous['operation_id']))
            else:
                raise BaseSyncBusy('Base entity has an unsettled operation; do not retry remote writes')
        values = (None,)*5 if ref is None else (ref.job_id, ref.attempt_no, ref.owner, ref.input_digest, ref.lifecycle_round)
        conn.execute('''INSERT INTO base_sync_operations(operation_id,base_digest,table_id,entity_type,entity_id,
            job_id,attempt_no,lease_owner,input_digest,lifecycle_round,state,created_at,updated_at,expires_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,'reserved',?,?,?)''',
            (op, base_digest, table, entity_type, entity_id, *values, stamp, stamp,
             (datetime.now(UTC)+timedelta(seconds=120)).isoformat()))
    return op


def _prepare(conn, op):
    from .base_sync import _entity_payload
    with transaction(conn):
        row = _owns(conn, op, expected='reserved')
        fields, version = _entity_payload(conn, row['entity_type'], row['entity_id'])
        content = digest(fields)
        expected = job_input_digest(entity_type=row['entity_type'], entity_id=row['entity_id'], mirror=content,
                                    base_digest=row['base_digest'], table_id=row['table_id'])
        if row['job_id'] is not None and row['input_digest'] != expected:
            raise BaseSyncInputChanged('Base job input changed; enqueue a new snapshot')
        mapping = conn.execute('''SELECT * FROM base_mappings WHERE entity_type=? AND entity_id=?
            AND table_id=? AND base_digest=?''',
            (row['entity_type'], row['entity_id'], row['table_id'], row['base_digest'])).fetchone()
        # A stale worker may have created a record without being allowed to
        # publish its mapping. Reuse its exact completed response identity,
        # rather than depending on an eventually consistent search to find it.
        settled = conn.execute('''SELECT record_id FROM base_sync_operations
            WHERE base_digest=? AND table_id=? AND entity_type=? AND entity_id=?
              AND state='settled' AND record_id IS NOT NULL ORDER BY rowid DESC LIMIT 1''',
            (row['base_digest'], row['table_id'], row['entity_type'], row['entity_id'])).fetchone()
        record = mapping['record_id'] if mapping else settled['record_id'] if settled else None
        conn.execute('''UPDATE base_sync_operations SET state='prepared',fields_json=?,target_version=?,
            request_digest=?,record_id=?,updated_at=? WHERE operation_id=? AND state='reserved' ''',
            (canonical_json(fields), version, content, record, iso_now(), op))
        return dict(conn.execute('SELECT * FROM base_sync_operations WHERE operation_id=?', (op,)).fetchone()), bool(mapping and mapping['mirrored_digest'] == content)


def _dispatch(conn, config, op, record, body):
    from .base_sync import _entity_payload, ENTITY_TABLE_KEYS
    with transaction(conn):
        _enabled(conn, config)
        row = _owns(conn, op, expected='prepared')
        if (digest(config.raw['base']['app_token']) != row['base_digest']
                or str(config.raw['base'][ENTITY_TABLE_KEYS[row['entity_type']]]) != row['table_id']):
            raise BaseSyncInputChanged('Base destination changed before dispatch')
        fields, version = _entity_payload(conn, row['entity_type'], row['entity_id'])
        if version != row['target_version'] or digest(fields) != row['request_digest']:
            raise BaseSyncInputChanged('Base snapshot changed before dispatch; prepare again')
        write_digest = digest({'base_digest': row['base_digest'], 'table_id': row['table_id'], 'body': body})
        conn.execute("UPDATE base_sync_operations SET state='dispatched',record_id=?,write_digest=?,updated_at=? WHERE operation_id=? AND state='prepared'",
                     (record, write_digest, iso_now(), op))


def _finish(conn, op, record_id, *, changed):
    with transaction(conn):
        row = conn.execute('SELECT * FROM base_sync_operations WHERE operation_id=?', (op,)).fetchone()
        if row is None or row['state'] != ('dispatched' if changed else 'prepared'):
            raise BaseSyncSuperseded('Base completion generation changed')
        ref = _ref(row)
        current = ref is None or ref.current(conn) is not None
        result = {'entity_type': row['entity_type'], 'entity_id': row['entity_id'], 'record_id': record_id,
                  'version': row['target_version'], 'digest': row['request_digest'], 'changed': changed,
                  'operation_id': op, 'superseded': not current}
        stamp = iso_now()
        if current:
            conn.execute('''INSERT INTO base_mappings(entity_type,entity_id,table_id,record_id,
                mirrored_version,mirrored_digest,updated_at,base_digest) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(entity_type,entity_id) DO UPDATE SET table_id=excluded.table_id,
                record_id=excluded.record_id,mirrored_version=excluded.mirrored_version,
                mirrored_digest=excluded.mirrored_digest,updated_at=excluded.updated_at,base_digest=excluded.base_digest''',
                (row['entity_type'], row['entity_id'], row['table_id'], record_id, row['target_version'], row['request_digest'], stamp, row['base_digest']))
            if ref is not None:
                updated = conn.execute('''UPDATE jobs SET state='succeeded',output_digest=?,exit_code=0,
                    lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=?,updated_at=? WHERE job_id=?
                    AND state='running' AND attempt_no=? AND lease_owner=? AND input_digest=? AND lifecycle_round=?''',
                    (row['request_digest'], stamp, stamp, ref.job_id, ref.attempt_no, ref.owner, ref.input_digest, ref.lifecycle_round)).rowcount
                if updated != 1:
                    raise BaseSyncSuperseded('Base completion CAS failed')
                recorded = conn.execute('''UPDATE job_attempts SET ended_at=?,result='succeeded',detail_json=?
                    WHERE job_id=? AND attempt_no=? AND worker_id=? AND ended_at IS NULL''',
                    (stamp, canonical_json(result), ref.job_id, ref.attempt_no, ref.owner)).rowcount
                if recorded != 1:
                    raise BaseSyncSuperseded('Base attempt completion record changed')
        elif ref is not None:
            conn.execute('''UPDATE job_attempts SET detail_json=json_set(coalesce(detail_json,'{}'),
                '$.base_remote_result',json(?)) WHERE job_id=? AND attempt_no=? AND worker_id=?''',
                (canonical_json(result), ref.job_id, ref.attempt_no, ref.owner))
        # Even a stale local attempt can carry a completed remote response. Keep
        # its evidence on its immutable operation, never on the successor row.
        conn.execute("UPDATE base_sync_operations SET state='settled',record_id=?,result_json=?,updated_at=? WHERE operation_id=?",
                     (record_id, canonical_json(result), stamp, op))
        return result


def synchronize(conn, config, *, entity_type, entity_id, runner, attempt_ref=None):
    from .base_sync import ENTITY_PRIMARY_FIELDS, _remote_exact_record_ids
    op = _reserve(conn, config, entity_type, entity_id, attempt_ref)
    try:
        row, unchanged = _prepare(conn, op)
        if unchanged:
            return _finish(conn, op, row['record_id'], changed=False)
        prefix = ['base']
        common = ['--base-token', config.raw['base']['app_token'], '--table-id', row['table_id']]
        suffix = ['--format', 'json', '--as', 'user']
        record = row['record_id']
        if record is None:
            primary = ENTITY_PRIMARY_FIELDS[entity_type]
            found = runner(prefix + ['+record-search'] + common + ['--keyword', entity_id, '--search-field', primary] + suffix)
            ids = _remote_exact_record_ids(found.data, primary_field=primary, entity_id=entity_id)
            if len(ids) > 1:
                raise BaseSyncError('Base contains duplicate records for the business key')
            record = ids[0] if ids else None
        fields = json.loads(row['fields_json'])
        action = '+record-batch-update' if record else '+record-batch-create'
        body = {'update_records': {record: fields}} if record else {'create_records': [fields]}
        _dispatch(conn, config, op, record, body)
        response = runner(prefix + [action] + common + ['--json', canonical_json(body)] + suffix)
        if record is None:
            data = response.data if isinstance(response.data, dict) else {}
            ids = data.get('record_id_list') or data.get('record_ids') or []
            if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str) or not ids[0]:
                raise BaseSyncError('Base create did not return exactly one record ID')
            record = ids[0]
        return _finish(conn, op, record, changed=True)
    except BaseException as error:
        # A dispatched request is conservatively unknown even when the local
        # process threw before its result could be parsed or committed.
        with transaction(conn):
            conn.execute('''UPDATE base_sync_operations SET state=CASE WHEN state='dispatched' THEN 'unknown' ELSE 'cancelled' END,
                result_json=?,updated_at=? WHERE operation_id=? AND state IN ('reserved','prepared','dispatched')''',
                (canonical_json({'error': type(error).__name__}), iso_now(), op))
        raise


def resume_unblocked(conn, config, *, limit=128):
    """Requeue only local waiters; never settle an external operation by timeout."""
    from .base_sync import ENTITY_TABLE_KEYS
    if not conn.in_transaction:
        raise ValueError('Base waiter recovery requires control transaction')
    rows = conn.execute("""SELECT job_id,context_json FROM jobs WHERE job_type='base_sync'
        AND state='waiting' AND error_class='BaseSyncBusy' AND attempt_no<max_attempts
        ORDER BY updated_at,job_id LIMIT ?""", (limit,)).fetchall()
    for row in rows:
        context = json.loads(row['context_json'])
        kind, entity = context.get('entity_type'), context.get('entity_id')
        if kind not in ENTITY_TABLE_KEYS or not isinstance(entity, str):
            continue
        if _legacy_hold(conn, kind, entity):
            continue
        occupied = conn.execute('''SELECT 1 FROM base_sync_operations WHERE base_digest=? AND table_id=?
            AND entity_type=? AND entity_id=? AND state IN ('reserved','prepared','dispatched','unknown')''',
            (digest(config.raw['base']['app_token']), str(config.raw['base'][ENTITY_TABLE_KEYS[kind]]), kind, entity)).fetchone()
        if occupied is None:
            stamp = iso_now()
            conn.execute("UPDATE jobs SET state='queued',available_at=?,updated_at=?,error_class=NULL WHERE job_id=? AND state='waiting' AND error_class='BaseSyncBusy'",
                         (stamp, stamp, row['job_id']))
