from k3_support.case_content_inventory import FIELDS, INDIRECT
from k3_support.retention_field_registry import BODY, MIXED, inventory_rules, classification
from k3_support.retention_field_registry import core_schema_audit


def test_all_counted_fields_have_explicit_non_authorizing_semantics():
    registered = dict(FIELDS)
    registered.update({table: value[0] for table, value in INDIRECT.items()})
    result = inventory_rules(registered)
    assert result['registered_fields_classified']
    assert not result['whole_schema_classified'] and not result['retirement_ready']
    assert all(not f['clear_allowed'] for f in result['fields'])
    for table in BODY:
        assert not set(BODY[table]) & set(MIXED.get(table, ()))


def test_unknown_columns_never_inherit_policy_from_suffix():
    assert classification('cases', 'title') == 'body_candidate'
    assert classification('approvals', 'requested_action_json') == 'mixed_content_and_control'
    assert classification('cases', 'new_payload_json') == 'unclassified'
    assert not inventory_rules({'cases': ('title', 'new_payload_json')})['registered_fields_classified']


def test_reviewed_core_schema_matches_and_new_field_is_not_implicitly_retained(conn):
    assert core_schema_audit(conn)['reviewed_schema_matches']
    assert classification('inbound_events', 'idempotency_key') == 'retained_metadata'
    assert classification('inbound_events', 'last_error') == 'body_candidate'
    conn.execute('ALTER TABLE evidence ADD COLUMN extra_note TEXT')
    result = core_schema_audit(conn)
    assert not result['reviewed_schema_matches']
    assert result['changes'] == [{'table':'evidence', 'field':'extra_note', 'reason':'unknown_column'}]


def test_core_schema_type_changes_are_detected():
    import sqlite3
    conn = sqlite3.connect(':memory:')
    try:
        conn.execute('CREATE TABLE evidence(evidence_id BLOB)')
        changes = core_schema_audit(conn)['changes']
        assert {'table':'evidence', 'field':'evidence_id', 'reason':'type_changed'} in changes
        assert {'table':'evidence', 'field':'result', 'reason':'missing_column'} in changes
    finally:
        conn.close()


def test_control_fences_and_provenance_are_retained_not_body_candidates(conn):
    for table, field in [('approvals','action_digest'), ('approvals','approver_identity'),
                         ('outbox','idempotency_key'), ('outbox','communication_fence'),
                         ('outbox','context_digest'), ('action_ledger','input_digest'),
                         ('case_sources','source_version')]:
        assert classification(table, field) == 'retained_metadata'
    assert classification('outbox', 'suppression_reason') == 'body_candidate'
    assert classification('outbox', 'payload_json') == 'mixed_content_and_control'
    assert core_schema_audit(conn)['reviewed_schema_matches']


def test_all_counted_tables_have_reviewed_metadata_and_types(conn):
    audit = core_schema_audit(conn)
    assert set(audit['reviewed_tables']) == set(FIELDS) | set(INDIRECT)
    assert audit['reviewed_schema_matches']
    assert not audit['whole_schema_classified']
