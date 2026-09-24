"""Bounded FK descendant inventory. Counts are not content classification."""
from collections import defaultdict


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def descendants(conn, case_id, *, max_rows=2000):
    """Read identifiers only; preserve composite-FK grouping and cycles.

    Caller owns the snapshot/time budget. Only incoming declared FKs are followed.
    JSON, external IDs and outgoing/shared parent references are not inferred.
    """
    if not isinstance(case_id, str) or not 1 <= len(case_id) <= 128:
        raise ValueError('invalid case identity')
    if type(max_rows) is not int or not 1 <= max_rows <= 10000:
        raise ValueError('invalid reference row budget')
    tables = {r[0]: r[1] or '' for r in conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    without_rowid = {r[1] for r in conn.execute('PRAGMA table_list') if r[0] == 'main' and r[4]}
    usable, unsupported, primary, keys = set(), [], {}, {}
    for table, sql in tables.items():
        columns = list(conn.execute(f'PRAGMA table_info({quote(table)})'))
        primary[table] = [c[1] for c in sorted(columns, key=lambda c: c[5]) if c[5]]
        names = {str(c[1]).lower() for c in columns}
        alias = next((name for name in ('rowid', '_rowid_', 'oid') if name not in names), None)
        keys[table] = primary[table] if table in without_rowid else ([alias] if alias else [])
        if not keys[table]:
            unsupported.append(table)
            continue
        usable.add(table)
    edges = defaultdict(list)
    canonical_tables = {table.lower(): table for table in usable}
    unresolved = []
    for child in sorted(usable):
        groups = defaultdict(list)
        for fk in conn.execute(f'PRAGMA foreign_key_list({quote(child)})'):
            groups[fk[0]].append(fk)
        for group in groups.values():
            group.sort(key=lambda fk: fk[1])
            parent = canonical_tables.get(group[0][2].lower(), group[0][2])
            if parent not in usable:
                unresolved.append([child, parent])
                continue
            pairs = []
            for index, fk in enumerate(group):
                target = fk[4] or (primary[parent][index] if index < len(primary[parent]) else None)
                if target is None:
                    unresolved.append([child, parent])
                    break
                pairs.append((fk[3], target))
            else:
                edges[parent].append((child, pairs))
    if 'cases' not in usable:
        raise ValueError('Case table has no supported stable row identity')
    root_columns = ','.join(quote(k) for k in keys['cases'])
    root = conn.execute(f'SELECT {root_columns} FROM cases WHERE case_id=?', (case_id,)).fetchone()
    if root is None:
        raise ValueError('case not found')
    reached = {'cases': {tuple(root)}}
    frontier = {'cases': {tuple(root)}}
    count, truncated = 1, False
    while frontier and not truncated:
        following = defaultdict(set)
        for parent, ids in frontier.items():
            ordered = sorted(ids, key=repr)
            for child, pairs in edges[parent]:
                # SQLite FKs use the parent column's collation/affinity. Putting
                # the child first would miss valid NOCASE parent references.
                joins = ' AND '.join(f'p.{quote(b)}=c.{quote(a)}' for a, b in pairs)
                batch_size = max(1, 200 // len(keys[parent]))
                for start in range(0, len(ordered), batch_size):
                    batch = ordered[start:start+batch_size]
                    match = '(' + ' AND '.join(f'p.{quote(k)}=?' for k in keys[parent]) + ')'
                    condition = ' OR '.join(match for _ in batch)
                    child_keys = ','.join(f'c.{quote(k)}' for k in keys[child])
                    parameters = [value for key in batch for value in key]
                    cursor = conn.execute(f'SELECT DISTINCT {child_keys} FROM {quote(child)} c '
                        f'JOIN {quote(parent)} p ON {joins} WHERE ({condition})', parameters)
                    for row in cursor:
                        known = reached.setdefault(child, set())
                        identity = tuple(row)
                        if identity in known:
                            continue
                        if count >= max_rows:
                            truncated = True
                            break
                        known.add(identity)
                        following[child].add(identity)
                        count += 1
                    cursor.close()
                    if truncated:
                        break
                if truncated:
                    break
            if truncated:
                break
        frontier = following
    return {'tables': [{'table': table, 'rows': len(ids)}
                       for table, ids in sorted(reached.items()) if ids],
            'rows': count, 'truncated': truncated,
            'unsupported_tables': sorted(unsupported), 'unresolved_foreign_keys': unresolved,
            'declared_fk_scan_complete': not truncated and not unsupported and not unresolved,
            'coverage_complete': False, 'deletion_allowed': False,
            'excluded': ['undeclared_references', 'outgoing_shared_parents', 'content_classification',
                         'files_and_external_copies']}
