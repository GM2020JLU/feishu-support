"""Transactional row-identity capture. Trigger bodies never parse source JSON."""

from .ids import canonical_json, digest
from .retention_reference_sources import identifier


def install_source(conn, source, *, verify_only=False):
    """Install one inventoried source inside the caller's write transaction.

    This does not authorize ready or imply backfill. Row tombstones persist so
    delete/reinsert and primary-key changes cannot reuse an old revision.
    """
    if not conn.in_transaction and not verify_only:
        raise ValueError('source capture requires a write transaction')
    if not source.primary_key:
        raise ValueError('source has no stable primary key')
    source_id = digest([source.table, source.column])
    existing = conn.execute('SELECT * FROM retention_reference_sources WHERE source_id=?',
                            (source_id,)).fetchone()
    values = (source.table, source.column, canonical_json(source.primary_key),
              canonical_json(source.kinds), source.schema_digest, source.extractor_version)
    if existing is not None:
        actual = tuple(existing[key] for key in ('table_name', 'column_name', 'key_spec',
                                                'kinds', 'schema_digest', 'extractor_version'))
        if actual != values:
            raise ValueError('source definition changed; rebuild required')
    else:
        if verify_only:
            raise ValueError('source capture is missing')
        conn.execute('''INSERT INTO retention_reference_sources
            (source_id,table_name,column_name,key_spec,kinds,schema_digest,extractor_version)
            VALUES(?,?,?,?,?,?,?)''', (source_id, *values))

    def capture(alias, deleted):
        key = 'json_array(' + ','.join(f'{alias}.{identifier(k)}' for k in source.primary_key) + ')'
        null_key = ' OR '.join(f'{alias}.{identifier(k)} IS NULL' for k in source.primary_key)
        # source_id is a generated hex digest, never caller-supplied SQL text.
        return f'''
            SELECT CASE WHEN {null_key} THEN RAISE(ABORT,'reference source key is null') END;
            INSERT INTO retention_reference_rows(source_id,row_key,revision,deleted,state)
                VALUES('{source_id}',{key},1,{deleted},'pending')
                ON CONFLICT(source_id,row_key) DO UPDATE SET
                    revision=revision+1,deleted={deleted},state='pending',error_class=NULL,shadow_revision=NULL;
            INSERT INTO retention_reference_dirty(source_id,row_key,revision)
                SELECT source_id,row_key,revision FROM retention_reference_rows
                WHERE source_id='{source_id}' AND row_key={key}
                ON CONFLICT(source_id,row_key) DO UPDATE SET revision=excluded.revision;
        '''
    definitions = {
        'insert': capture('NEW', 0),
        'delete': capture('OLD', 1),
        'update': capture('OLD', 1) + capture('NEW', 0),
    }
    for event, body in definitions.items():
        name = f'retention_capture_{source_id}_{event}'
        statement = f'CREATE TRIGGER {identifier(name)} AFTER {event.upper()} ON {identifier(source.table)} BEGIN {body} END'
        current = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if current:
            # SQLite drops redundant identifier quotes around the trigger name.
            normalized = statement.replace(identifier(name), name, 1)
            if current[0] not in (statement, normalized):
                raise ValueError('capture trigger changed')
        else:
            if verify_only:
                raise ValueError('capture trigger is missing')
            conn.execute(statement)
    return source_id
