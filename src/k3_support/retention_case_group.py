"""Bounded canonical/duplicate Case group, not a complete dependency graph."""
from .ids import digest


def resolve(conn, case_id, *, limit=100):
    if not isinstance(case_id, str) or not case_id or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('invalid Case group request')
    conn.execute('SAVEPOINT retention_case_group')
    try:
        pending, members, issues = [case_id], {}, set()
        while pending:
            current = pending.pop()
            if current in members:
                continue
            if len(members) >= limit:
                issues.add('group_limit_exceeded')
                break
            row = conn.execute('''SELECT case_id,canonical_case_id,state,version,lifecycle_round
                FROM cases WHERE case_id=?''', (current,)).fetchone()
            if row is None:
                issues.add('missing_canonical_case')
                continue
            members[current] = dict(row)
            if row['canonical_case_id']:
                pending.append(row['canonical_case_id'])
            children = conn.execute('SELECT case_id FROM cases WHERE canonical_case_id=? ORDER BY case_id LIMIT ?',
                                    (current, limit+1)).fetchall()
            if len(children) > limit:
                issues.add('group_limit_exceeded')
            pending.extend(child[0] for child in children[:limit])
        for start in members:
            path, current = set(), start
            while current in members:
                if current in path:
                    issues.add('canonical_cycle')
                    break
                path.add(current)
                current = members[current]['canonical_case_id']
        rows = [members[key] for key in sorted(members)]
        return {'members': rows, 'issues': sorted(issues), 'canonical_group_complete': not issues,
                'binding_digest': digest({'members':rows, 'issues':sorted(issues)}),
                'dependency_coverage_complete': False, 'clear_allowed': False,
                'scope':'canonical_case_links_only'}
    finally:
        conn.execute('RELEASE retention_case_group')
