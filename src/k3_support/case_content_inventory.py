"""Read-only registered content inventory; never grants retirement authority."""

from .ids import digest
from .case_reference_graph import descendants
from .retention_field_registry import inventory_rules, core_schema_audit
from .retention_json_fields import outbox_fields, redact_outbox_text
from .retention_case_group import resolve as resolve_case_group
from .broker_remote_state import UNSETTLED
import math
import json
import sqlite3
import time


def bounded_preview(conn, case_id, *, seconds=3, row_limit=1000):
    """For a dedicated request connection only; owns its progress handler.

    Bounds SQLite VM work cooperatively, not blocking I/O or lock waits.
    """
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 5:
        raise ValueError('inventory budget must be 0..5 seconds')
    deadline = time.monotonic() + seconds
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        result = preview(conn, case_id, row_limit=row_limit)
        if time.monotonic() >= deadline:
            raise ValueError('统计超过时间预算，未返回部分结果，也未执行清理')
        return result
    except sqlite3.OperationalError as error:
        if getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
            raise ValueError('统计超过时间预算，未返回部分结果，也未执行清理') from None
        raise
    finally:
        conn.set_progress_handler(None, 0)

# Explicit field names, not a suffix-based deletion heuristic. These are known
# content surfaces only; later schemas and external copies remain unclassified.
FIELDS = {
    'cases': ('title', 'next_action'),
    'case_events': ('detail_json',),
    'case_sources': ('title', 'url', 'metadata_json'),
    'evidence': ('claim', 'result'),
    'case_suggestions': ('content_json',),
    'approvals': ('requested_action_json', 'decision_text'),
    'outbox': ('payload_json', 'remote_result_json', 'suppression_reason'),
    'action_ledger': ('result_json',),
    'case_handoffs': ('content_json',),
    'case_lifecycle_actions': ('result_json',),
    'case_rounds': ('reason',),
    'codex_reviews': ('manifest_json', 'independent_checks_json', 'hermes_output_json', 'error'),
    'conversation_contexts': ('query_text', 'facts_json', 'pending_associations_json', 'conflicts_json'),
    'delivery_blocks': ('next_action', 'reason'),
    'diagnostic_snapshots': ('facts_json', 'missing_json'),
    'incident_cluster_members': ('reason',),
    'jobs': ('context_json',),
    'knowledge_feedback': ('detail',),
    'locks': ('metadata_json',),
    'mail_items': ('subject', 'body_preview', 'classification_reason', 'requested_action'),
    'meeting_create_attempts': ('action_json', 'target_json', 'adopted_target_json'),
    'meeting_previews': ('action_json', 'remote_result_json'),
    'model_budget_attempts': ('receipt_json',),
    'professional_validation_runs': ('environment_json',),
    'route_decisions': ('clarification_question', 'profile_snapshot_json', 'review_note', 'knowledge_runtime_json', 'repository_hints_json'),
}

INDIRECT = {
    'inbound_events': (('payload_json', 'last_error'), '''event_pk IN (
        SELECT source_event_pk FROM case_events WHERE case_id=:case
        UNION SELECT source_event_pk FROM conversation_turns WHERE case_id=:case
        UNION SELECT source_event_pk FROM outbox WHERE case_id=:case
        UNION SELECT focus_event_pk FROM conversation_contexts WHERE case_id=:case
        UNION SELECT m.event_pk FROM conversation_context_members m
          JOIN conversation_contexts c ON c.context_id=m.context_id WHERE c.case_id=:case
        UNION SELECT a.source_event_pk FROM conversation_anchor_aliases a
          JOIN conversation_contexts c ON c.context_id=a.context_id WHERE c.case_id=:case
        UNION SELECT e.event_pk FROM inbound_events e JOIN case_sources s
          ON s.stable_external_id IN (e.event_pk,e.external_id) WHERE s.case_id=:case)'''),
    'job_attempts': (('result', 'detail_json'),
                     'job_id IN (SELECT job_id FROM jobs WHERE case_id=:case)'),
    'operator_activities': (('detail_json',),
        'matched_turn_id IN (SELECT turn_id FROM conversation_turns WHERE case_id=:case)'),
    'mail_summary_membership': (('metadata_json',),
        'message_id IN (SELECT message_id FROM mail_items WHERE case_id=:case)'),
    'mail_digest_runs': (('ai_summary_json',), '''digest_id IN (
        SELECT digest_id FROM mail_summary_membership WHERE message_id IN
          (SELECT message_id FROM mail_items WHERE case_id=:case)
        UNION SELECT digest_id FROM mail_digest_links WHERE message_id IN
          (SELECT message_id FROM mail_items WHERE case_id=:case))'''),
    'mail_digest_links': (('error', 'message_app_link'),
        'message_id IN (SELECT message_id FROM mail_items WHERE case_id=:case)'),
}

# Conservative observable retention reasons, not a terminal-state authority.
INDIRECT['outbox'] = (FIELDS['outbox'], f'''case_id=:case
    OR context_id IN (SELECT context_id FROM conversation_contexts WHERE case_id=:case)
    OR turn_id IN (SELECT turn_id FROM conversation_turns WHERE case_id=:case)
    OR outbox_id IN (SELECT telegram_outbox_id FROM mail_digest_runs
                    WHERE {INDIRECT['mail_digest_runs'][1]})
    OR outbox_id IN (SELECT share_outbox_id FROM mail_digest_links
                    WHERE {INDIRECT['mail_digest_links'][1]})
    OR outbox_id IN (SELECT resolve_outbox_id FROM mail_digest_links
                    WHERE {INDIRECT['mail_digest_links'][1]})''')

HOLDS = {
    'case_not_closed': ('cases', "state NOT IN ('resolved','cancelled')"),
    'worker_exit_unverified': ('broker_execution_starts', 'NOT EXISTS(SELECT 1 FROM broker_service_exits e JOIN broker_execution_instances i ON i.grant_id=e.grant_id AND i.invocation_id=e.invocation_id WHERE e.grant_id=broker_execution_starts.grant_id)'),
    'remote_execution_unsettled': ('broker_remote_actions', f'request_id IN (SELECT a.request_id FROM broker_remote_actions a LEFT JOIN broker_remote_results r USING(request_id) WHERE {UNSETTLED})'),
    'board_cleanup_unsettled': ('broker_board_cleanup', "state != 'succeeded'"),
    'resource_lock_present': ('locks', '1'),
    'jobs_unsettled': ('jobs', "state NOT IN ('succeeded','failed','cancelled') OR lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL"),
    'approvals_open': ('approvals', "status IN ('requested','approved')"),
    'delivery_unsettled': ('outbox', "state NOT IN ('delivered','cancelled') OR lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL"),
    'actions_unsettled': ('action_ledger', "state NOT IN ('verified','failed','cancelled')"),
    'conversation_open': ('conversation_turns', "state != 'closed'"),
}

HOLD_SCOPES = {
    'broker_execution_starts': 'grant_id IN (SELECT g.grant_id FROM broker_grants g JOIN jobs j USING(job_id) WHERE j.case_id=:case)',
    'broker_remote_actions': 'grant_id IN (SELECT g.grant_id FROM broker_grants g JOIN jobs j USING(job_id) WHERE j.case_id=:case)',
    'broker_board_cleanup': 'grant_id IN (SELECT g.grant_id FROM broker_grants g JOIN jobs j USING(job_id) WHERE j.case_id=:case)',
}


def shared_source_count(conn, case_id):
    return conn.execute(f'''WITH selected AS (
        SELECT event_pk,external_id FROM inbound_events WHERE {INDIRECT['inbound_events'][1]}
    ) SELECT count(*) FROM selected e WHERE
        EXISTS(SELECT 1 FROM case_events r WHERE r.source_event_pk=e.event_pk AND r.case_id!=:case)
        OR EXISTS(SELECT 1 FROM conversation_turns r WHERE r.source_event_pk=e.event_pk AND r.case_id!=:case)
        OR EXISTS(SELECT 1 FROM outbox r WHERE r.source_event_pk=e.event_pk AND (r.case_id IS NULL OR r.case_id!=:case))
        OR EXISTS(SELECT 1 FROM case_sources r WHERE r.stable_external_id IN (e.event_pk,e.external_id) AND r.case_id!=:case)
        OR EXISTS(SELECT 1 FROM conversation_contexts c WHERE c.focus_event_pk=e.event_pk AND (c.case_id IS NULL OR c.case_id!=:case))
        OR EXISTS(SELECT 1 FROM conversation_context_members m JOIN conversation_contexts c USING(context_id)
            WHERE m.event_pk=e.event_pk AND (c.case_id IS NULL OR c.case_id!=:case))
        OR EXISTS(SELECT 1 FROM conversation_anchor_aliases a JOIN conversation_contexts c USING(context_id)
            WHERE a.source_event_pk=e.event_pk AND (c.case_id IS NULL OR c.case_id!=:case))
        ''', {'case': case_id}).fetchone()[0]


def knowledge_reference_count(conn, case_id):
    return conn.execute(f'''WITH selected AS (
        SELECT event_pk,external_id FROM inbound_events WHERE {INDIRECT['inbound_events'][1]}
    ) SELECT count(*) FROM knowledge_entries k WHERE k.canonical_case_id=:case
        OR EXISTS(SELECT 1 FROM knowledge_sources s JOIN selected e
            ON s.stable_external_id IN (e.event_pk,e.external_id)
            WHERE s.knowledge_id=k.knowledge_id)
        OR EXISTS(SELECT 1 FROM knowledge_sources s JOIN case_sources c
            ON s.stable_external_id=c.stable_external_id
            WHERE s.knowledge_id=k.knowledge_id AND c.case_id=:case)
        ''', {'case': case_id}).fetchone()[0]


def operational_holds(conn, case_id):
    """One scope definition for root and transitive member operational checks."""
    holds = []
    for reason, (table, predicate) in HOLDS.items():
        scope = HOLD_SCOPES.get(table, INDIRECT.get(table, ((), 'case_id=:case'))[1])
        count = conn.execute(
            f'SELECT count(*) FROM "{table}" WHERE ({scope}) AND ({predicate})',
            {'case': case_id}).fetchone()[0]
        if count:
            holds.append({'reason': reason, 'count': count})
    return holds


def dependency_holds(conn, case_id):
    # Even retired knowledge and mixed-Case digests retain source dependencies.
    counts = {
        'knowledge_source_reference': knowledge_reference_count(conn, case_id),
        'shared_source_messages': shared_source_count(conn, case_id),
        'mail_summary_reference': conn.execute(
            f"SELECT count(*) FROM mail_digest_runs WHERE {INDIRECT['mail_digest_runs'][1]}",
            {'case': case_id}).fetchone()[0],
    }
    return [{'reason': reason, 'count': count} for reason, count in counts.items() if count]


def group_holds(conn, group):
    """Observable holds per resolved member, not a full dependency clearance.

    Shared rows may occur under multiple members; counts must not be summed as
    unique records. The caller owns the consistent snapshot and query budget.
    """
    members = []
    for member in group['members']:
        members.append({'case_id': member['case_id'], 'case_state': member['state'],
                        'observed_holds': operational_holds(conn, member['case_id']),
                        'dependency_holds': dependency_holds(conn, member['case_id'])})
    return {'members': members, 'resolved_group_complete': group['canonical_group_complete'],
            'scope': 'observable_operational_and_selected_dependency_holds', 'counts_are_per_member': True,
            'dependency_coverage_complete': False, 'clear_allowed': False}


def preview(conn, case_id, *, row_limit=1000):
    if not isinstance(case_id, str) or not 1 <= len(case_id) <= 128:
        raise ValueError('invalid case identity')
    if type(row_limit) is not int or not 1 <= row_limit <= 10000:
        raise ValueError('row limit must be 1..10000')
    conn.execute('SAVEPOINT case_content_inventory')
    try:
        case = conn.execute('SELECT state,version,canonical_case_id FROM cases WHERE case_id=?',
                            (case_id,)).fetchone()
        if case is None:
            raise ValueError('case not found')
        registered = {table: fields for table, fields in FIELDS.items()}
        registered.update({table: item[0] for table, item in INDIRECT.items()})
        schema, unclassified = [], 0
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            table = row[0]
            quoted = '"' + table.replace('"', '""') + '"'
            for column in conn.execute(f'PRAGMA table_info({quoted})'):
                schema.append([table, column[1], column[2], column[3], column[5]])
                # This is intentionally not a content heuristic. Metadata is also
                # unclassified until explicitly reviewed, rather than assumed safe.
                if column[1] not in registered.get(table, ()):
                    unclassified += 1
        surfaces = []
        selectors = {table: (fields, 'case_id=:case') for table, fields in FIELDS.items()}
        selectors.update(INDIRECT)
        for table, (fields, predicate) in selectors.items():
            # Identifiers come exclusively from the static registry. Only lengths
            # cross the database boundary, never content strings.
            lengths = ','.join(f'length(CAST("{field}" AS BLOB))' for field in fields)
            rows = conn.execute(f'SELECT {lengths} FROM "{table}" WHERE {predicate} ORDER BY rowid LIMIT :limit',
                                {'case': case_id, 'limit': row_limit + 1}).fetchall()
            surfaces.append({'table': table, 'fields': list(fields),
                             'rows_counted': min(len(rows), row_limit),
                             'bytes_counted': sum(sum(v or 0 for v in row) for row in rows[:row_limit]),
                             'truncated': len(rows) > row_limit})
        linked = conn.execute('SELECT count(*) FROM cases WHERE canonical_case_id=?', (case_id,)).fetchone()[0]
        holds = operational_holds(conn, case_id)
        if linked or case['canonical_case_id'] is not None:
            holds.append({'reason': 'linked_case_review_required', 'count': linked + int(case['canonical_case_id'] is not None)})
        holds.extend(dependency_holds(conn, case_id))
        json_fields = {'recognized_rows': 0, 'unclassified_rows': 0, 'body_bytes': 0,
                       'release_provenance_review_rows': 0,
                       'candidate_transform_rows': 0, 'candidate_removed_bytes': 0,
                       'truncated': next(surface['truncated'] for surface in surfaces if surface['table'] == 'outbox'),
                       'clear_allowed': False, 'scope': 'bounded_feishu_text_outbox_only'}
        for record in conn.execute(f'''SELECT channel,action_type,
            CASE WHEN length(CAST(payload_json AS BLOB))<=262144 THEN payload_json END AS payload
            FROM outbox WHERE {INDIRECT['outbox'][1]} ORDER BY rowid LIMIT :limit''',
            {'case': case_id, 'limit': row_limit}):
            classified = outbox_fields(channel=record['channel'], action_type=record['action_type'],
                                       payload_json=record['payload'])
            json_fields['recognized_rows' if classified['shape_recognized'] else 'unclassified_rows'] += 1
            json_fields['body_bytes'] += classified['body_bytes']
            json_fields['release_provenance_review_rows'] += int(
                classified.get('release_provenance_review_required', False))
            if classified['shape_recognized']:
                transformed = redact_outbox_text(channel=record['channel'], action_type=record['action_type'],
                    payload_json=record['payload'], expected_digest=classified['input_digest'])
                json_fields['candidate_transform_rows'] += 1
                json_fields['candidate_removed_bytes'] += transformed['removed_body_bytes']
        group = resolve_case_group(conn, case_id)
        from .retention_fact_fields import fact_json_fields, redact_fact_content
        fact_inventory = {'recognized_rows':0,'unclassified_rows':0,'body_bytes':0,
                          'candidate_transform_rows':0,'candidate_removed_bytes':0,
                          'clear_allowed':False,'truncated':False}
        fact_rows = conn.execute('''SELECT CASE WHEN length(CAST(facts_json AS BLOB))<=262144
            THEN facts_json END FROM conversation_contexts WHERE case_id=?
            ORDER BY rowid LIMIT ?''', (case_id,row_limit+1)).fetchall()
        fact_inventory['truncated'] = len(fact_rows)>row_limit
        for row in fact_rows[:row_limit]:
            result = fact_json_fields(row[0])
            fact_inventory['recognized_rows' if result['shape_recognized'] else 'unclassified_rows'] += 1
            fact_inventory['body_bytes'] += result['body_bytes']
            if result['shape_recognized']:
                transformed = redact_fact_content(json.loads(row[0]),expected_digest=result['input_digest'])
                fact_inventory['candidate_transform_rows'] += 1
                fact_inventory['candidate_removed_bytes'] += transformed['removed_body_bytes']
        group_operational_holds = group_holds(conn, group)
        return {'case_id': case_id, 'case_state': case['state'], 'case_version': case['version'],
                'canonical_case_present': case['canonical_case_id'] is not None,
                'linked_cases': linked, 'surfaces': surfaces, 'observed_holds': holds, 'read_only': True,
                'deletion_allowed': False, 'coverage_complete': False,
                'schema_digest': digest(schema), 'unclassified_columns': unclassified,
                'field_registry': inventory_rules(registered),
                'core_field_schema': core_schema_audit(conn),
                'outbox_json_fields': json_fields,
                'context_fact_fields': fact_inventory,
                'canonical_group': group,
                'canonical_group_holds': group_operational_holds,
                'declared_reference_graph': descendants(conn, case_id, max_rows=row_limit),
                'uncovered': ['unregistered_text_and_json_references', 'later_schema_content',
                              'knowledge_dependencies', 'files_exports_backups', 'external_copies']}
    finally:
        conn.execute('RELEASE case_content_inventory')
