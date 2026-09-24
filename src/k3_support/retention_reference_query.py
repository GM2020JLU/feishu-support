"""Indexed conservative references; call in the target's read/write snapshot."""

import json

from .retention_reference_extract import reference_digest
from .retention_reference_index import inspect_build


def dependencies(conn, events):
    if not conn.in_transaction:
        raise ValueError('reference query requires a target snapshot')
    status = inspect_build(conn)
    found = {event['event_pk']: [] for event in events}
    if not status['ready']:
        return found, [{'scan_incomplete': True, 'reason': 'reference_index_not_ready',
                        'blockers': status['blockers']}]
    for event in events:
        targets = sorted({reference_digest(event['event_pk']), reference_digest(event['external_id'])})
        placeholders = ','.join('?' for _ in targets)
        rows = conn.execute(f'''SELECT DISTINCT s.source_id,s.table_name,s.column_name,s.key_spec,s.kinds,e.row_key
            FROM retention_reference_edges e JOIN retention_reference_sources s USING(source_id)
            WHERE e.target_digest IN ({placeholders})''', targets)
        counts = {}
        for row in rows:
            kinds = json.loads(row['kinds'])
            # Exclude only a proven self-reference in this inbound row's JSON.
            # Identity/hash collisions in other rows remain protective.
            if (kinds == ['json'] and row['table_name'] == 'inbound_events'
                    and json.loads(row['key_spec']) == ['event_pk']
                    and json.loads(row['row_key']) == [event['event_pk']]):
                continue
            key = (row['table_name'], row['column_name'], tuple(kinds))
            counts[key] = counts.get(key, 0)+1
        for (table, column, kinds), count in sorted(counts.items()):
            item = {'table': table, 'column': column, 'count': count}
            if kinds == ('json',):
                item['kind'] = 'json_id_reference'
            found[event['event_pk']].append(item)
    return found, []
