from k3_support.store import create_case
from k3_support.retention_case_group import resolve


def seed(conn):
    return [create_case(conn, title='PRIVATE TITLE', case_type='bug', severity='P3', confidence=.9)[0]
            for _ in range(4)]


def test_bidirectional_transitive_group_excludes_unrelated_and_invalidates_on_change(conn):
    root, child, grandchild, other = seed(conn)
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (root, child))
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (child, grandchild))
    before = conn.serialize()
    value = resolve(conn, child)
    assert {r['case_id'] for r in value['members']} == {root, child, grandchild}
    assert value['canonical_group_complete'] and not value['clear_allowed']
    assert 'PRIVATE TITLE' not in str(value) and other not in str(value)
    assert conn.serialize() == before
    conn.execute('UPDATE cases SET version=version+1 WHERE case_id=?', (root,))
    assert resolve(conn, child)['binding_digest'] != value['binding_digest']


def test_cycles_and_limits_never_report_complete(conn):
    a, b, c, _ = seed(conn)
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (a, b))
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (b, c))
    assert 'group_limit_exceeded' in resolve(conn, a, limit=1)['issues']
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (c, a))
    value = resolve(conn, a)
    assert value['issues'] == ['canonical_cycle'] and not value['canonical_group_complete']


def test_missing_root_is_not_an_empty_complete_group(conn):
    result = resolve(conn, 'missing')
    assert result['issues'] == ['missing_canonical_case']
    assert not result['canonical_group_complete']


def test_canonical_child_lookup_uses_covering_index_without_sort(conn):
    root, child, _, _ = seed(conn)
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (root,child))
    plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN SELECT case_id FROM cases WHERE canonical_case_id=? ORDER BY case_id LIMIT ?',
                                         (root,101))]
    assert any('SEARCH' in line and 'idx_cases_canonical_group' in line for line in plan)
    assert not any('TEMP B-TREE' in line or 'SCAN cases' in line for line in plan)
    assert {row['case_id'] for row in resolve(conn,root)['members']} == {root,child}


def test_case_to_execution_grants_reuses_existing_indexes(conn):
    # Jobs and grants already have case/job-prefix unique indexes. Do not add
    # redundant indexes merely because their names differ from query columns.
    plan = [row[3] for row in conn.execute('''EXPLAIN QUERY PLAN
        SELECT g.grant_id FROM broker_grants g JOIN jobs j USING(job_id) WHERE j.case_id=?''', ('fixture',))]
    assert any('SEARCH j' in line and 'case_id=?' in line for line in plan)
    assert any('SEARCH g' in line and 'job_id=?' in line for line in plan)
    assert not any('SCAN j' in line or 'SCAN g' in line for line in plan)


def test_index_upgrade_preserves_cases_and_is_idempotent(conn):
    from k3_support.db import migrate
    root, child, _, _ = seed(conn)
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (root,child))
    before = [dict(row) for row in conn.execute('SELECT * FROM cases ORDER BY case_id')]
    conn.execute('DROP INDEX idx_cases_canonical_group')
    conn.execute('DELETE FROM schema_migrations WHERE version=93')
    assert migrate(conn) == [93]
    assert [dict(row) for row in conn.execute('SELECT * FROM cases ORDER BY case_id')] == before
    snapshot = conn.serialize()
    assert migrate(conn) == []
    assert conn.serialize() == snapshot
    assert {row['case_id'] for row in resolve(conn,root)['members']} == {root,child}


def test_expired_resource_lock_on_child_remains_a_group_hold(conn):
    from k3_support.case_content_inventory import preview
    root, child, _, _ = seed(conn)
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (root, child))
    conn.execute('''INSERT INTO locks(lock_key,owner,case_id,scope,acquired_at,expires_at,heartbeat_at)
        VALUES('board-fixture','fixture',?,'board','2000-01-01','2000-01-02','2000-01-01')''', (child,))
    before = conn.serialize()
    result = preview(conn, root)
    child_holds = next(row['observed_holds'] for row in result['canonical_group_holds']['members'] if row['case_id']==child)
    assert {'reason':'resource_lock_present','count':1} in child_holds
    assert {'reason':'resource_lock_present','count':1} in preview(conn, child)['observed_holds']
    assert conn.serialize() == before


def test_inventory_checks_indirect_deliveries_of_transitive_members(conn):
    from k3_support.case_content_inventory import preview

    root, child, grandchild, other = seed(conn)
    conn.execute("UPDATE cases SET state='resolved'")
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (root, child))
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (child, grandchild))
    conn.execute('''INSERT INTO conversation_contexts(context_id,chat_id,chat_type,case_id,
        input_digest,created_at,updated_at) VALUES('group-ctx','chat','p2p',?,'d','now','now')''',
        (grandchild,))
    conn.execute('''INSERT INTO outbox(outbox_id,channel,action_type,destination,payload_json,
        idempotency_key,state,context_id,created_at,updated_at)
        VALUES('group-send','telegram','reply','private','{"text":"SECRET"}',
        'group-key','pending','group-ctx','now','now')''')
    before = conn.serialize()
    result = preview(conn, root)['canonical_group_holds']
    members = {item['case_id']: item for item in result['members']}
    assert set(members) == {root, child, grandchild}
    assert {'reason': 'delivery_unsettled', 'count': 1} in members[grandchild]['observed_holds']
    assert not members[root]['observed_holds']
    assert result['resolved_group_complete'] and not result['clear_allowed']
    assert not result['dependency_coverage_complete']
    assert 'SECRET' not in str(result) and conn.serialize() == before
    conn.execute("UPDATE outbox SET state='delivered' WHERE outbox_id='group-send'")
    updated = preview(conn, root)['canonical_group_holds']
    assert not any(item['observed_holds'] for item in updated['members'])


def test_closed_root_does_not_hide_open_child(conn):
    from k3_support.case_content_inventory import preview
    root, child, _, _ = seed(conn)
    conn.execute("UPDATE cases SET state='resolved' WHERE case_id=?", (root,))
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (root, child))
    result = preview(conn, root)
    members = {row['case_id']: row for row in result['canonical_group_holds']['members']}
    assert not any(h['reason']=='case_not_closed' for h in members[root]['observed_holds'])
    assert {'reason':'case_not_closed','count':1} in members[child]['observed_holds']


def test_child_knowledge_reference_is_visible_from_root(conn):
    from test_knowledge_feedback import approved_knowledge
    from k3_support.case_content_inventory import preview
    root, child, _, _ = seed(conn)
    conn.execute('UPDATE cases SET canonical_case_id=? WHERE case_id=?', (root, child))
    approved_knowledge(conn, child)
    before = conn.serialize()
    result = preview(conn, root)
    member = next(row for row in result['canonical_group_holds']['members'] if row['case_id']==child)
    assert {'reason':'knowledge_source_reference','count':1} in member['dependency_holds']
    assert not result['canonical_group_holds']['dependency_coverage_complete']
    assert conn.serialize() == before
