"""Versioned field semantics, independent of SQL selectors and clear authority.

Body candidates still require reference/lifecycle checks and retired consumers.
Mixed fields must not be cleared wholesale: they bind audit or authorization.
Unknown fields fail closed, including ordinary metadata not yet reviewed here.
"""
from .ids import digest

VERSION = 'referenced-content-fields-v4'
BODY = {
    'cases': ('title', 'next_action'),
    'case_sources': ('title',),
    'evidence': ('claim',),
    'approvals': ('decision_text',),
    'case_rounds': ('reason',),
    'codex_reviews': ('error',),
    'conversation_contexts': ('query_text',),
    'delivery_blocks': ('next_action', 'reason'),
    'incident_cluster_members': ('reason',),
    'knowledge_feedback': ('detail',),
    'mail_items': ('subject', 'body_preview', 'classification_reason', 'requested_action'),
    'route_decisions': ('clarification_question', 'review_note'),
    'mail_digest_links': ('error',),
    'inbound_events': ('last_error',),
    'outbox': ('suppression_reason',),
}

# Identity, deduplication, fencing and provenance columns are retained, not body
# replacements. Paths remain references subject to their independent lifecycle.
METADATA = {
    'cases': ('case_id', 'type', 'severity', 'confidence', 'state', 'canonical_case_id',
              'requester_id', 'requester_chat_id', 'disclosure_class', 'owner',
              'active_job_id', 'active_worktree', 'active_session_id', 'last_public_update_at',
              'created_at', 'created_epoch', 'updated_at', 'updated_epoch', 'resolved_at',
              'version', 'outcome', 'outcome_provenance', 'lifecycle_round', 'last_material_progress_at'),
    'inbound_events': ('event_pk', 'source', 'identity', 'external_id', 'idempotency_key',
                       'sender_id', 'chat_id', 'thread_id', 'occurred_at', 'occurred_epoch',
                       'received_at', 'received_epoch', 'raw_artifact_path', 'status',
                       'lease_owner', 'lease_expires_at', 'attempt_count', 'next_attempt_at',
                       'claim_token', 'processing_started_at', 'heartbeat_at'),
    'evidence': ('evidence_id', 'case_id', 'source_id', 'evidence_layer', 'freshness_at',
                 'visibility', 'artifact_hash', 'created_at'),
    'approvals': ('approval_id', 'approval_type', 'case_id', 'session_id', 'status',
                  'action_digest', 'requested_at', 'expires_at', 'decided_at', 'approver_channel',
                  'approver_identity', 'approval_message_id', 'consumed_at', 'created_at',
                  'updated_at', 'lifecycle_round'),
    'outbox': ('outbox_id', 'channel', 'action_type', 'destination', 'idempotency_key', 'state',
               'lease_owner', 'lease_expires_at', 'attempt_count', 'next_attempt_at',
               'remote_message_id', 'delivered_at', 'case_id', 'source_event_pk', 'created_at',
               'updated_at', 'turn_id', 'turn_revision', 'communication_fence', 'not_before',
               'global_outbound_fence', 'claim_token', 'dispatch_started_at', 'effects_finalized_at',
               'lifecycle_round', 'context_id', 'context_revision', 'context_digest'),
    'action_ledger': ('action_key', 'action_type', 'case_id', 'state', 'input_digest', 'remote_id',
                      'started_at', 'finished_at', 'created_at', 'updated_at'),
    'case_sources': ('source_id', 'case_id', 'source_type', 'stable_external_id', 'source_version',
                     'visibility', 'requester_access', 'authority', 'updated_at'),
}
NON_TEXT = {'cases': {'confidence':'REAL', 'created_epoch':'INTEGER', 'updated_epoch':'INTEGER',
                       'version':'INTEGER', 'lifecycle_round':'INTEGER'},
            'inbound_events': {'occurred_epoch':'INTEGER', 'received_epoch':'INTEGER', 'attempt_count':'INTEGER'},
            'approvals': {'lifecycle_round':'INTEGER'},
            'outbox': {'attempt_count':'INTEGER', 'turn_revision':'INTEGER', 'communication_fence':'INTEGER',
                       'global_outbound_fence':'INTEGER', 'lifecycle_round':'INTEGER', 'context_revision':'INTEGER'},
            'case_sources': {'authority':'REAL'}}
MIXED = {
    'case_events': ('detail_json',),
    'case_sources': ('url', 'metadata_json'),
    'evidence': ('result',),
    'case_suggestions': ('content_json',),
    'approvals': ('requested_action_json',),
    'outbox': ('payload_json', 'remote_result_json'),
    'action_ledger': ('result_json',),
    'case_handoffs': ('content_json',),
    'case_lifecycle_actions': ('result_json',),
    'codex_reviews': ('manifest_json', 'independent_checks_json', 'hermes_output_json'),
    'conversation_contexts': ('facts_json', 'pending_associations_json', 'conflicts_json'),
    'diagnostic_snapshots': ('facts_json', 'missing_json'),
    'jobs': ('context_json',),
    'locks': ('metadata_json',),
    'meeting_create_attempts': ('action_json', 'target_json', 'adopted_target_json'),
    'meeting_previews': ('action_json', 'remote_result_json'),
    'model_budget_attempts': ('receipt_json',),
    'professional_validation_runs': ('environment_json',),
    'route_decisions': ('profile_snapshot_json', 'knowledge_runtime_json', 'repository_hints_json'),
    'inbound_events': ('payload_json',),
    'job_attempts': ('result', 'detail_json'),
    'operator_activities': ('detail_json',),
    'mail_summary_membership': ('metadata_json',),
    'mail_digest_runs': ('ai_summary_json',),
    'mail_digest_links': ('message_app_link',),
}


# Explicit names/types reviewed against the current counted surfaces. This is
# not generated from the live schema: new columns remain unknown.
_MORE_METADATA = {
    'case_events': 'event_id case_id sequence:INTEGER event_type actor_type actor_id source_event_pk before_state after_state idempotency_key created_at created_epoch:INTEGER',
    'case_handoffs': 'review_id case_id lifecycle_round:INTEGER created_at',
    'case_lifecycle_actions': 'external_id case_id action request_digest actor_id created_at',
    'case_rounds': 'case_id round_number:INTEGER started_at actor_id initial_case_version:INTEGER control_fence:INTEGER',
    'case_suggestions': 'suggestion_id case_id kind confidence:REAL evidence_ids_json policy_version status created_at',
    'codex_reviews': 'review_id job_id case_id status result_digest evidence_ids_json hermes_input_digest decision_id created_at updated_at',
    'conversation_contexts': 'context_id chat_id chat_type requester_id case_id lifecycle_round:INTEGER revision:INTEGER input_digest projected_revision:INTEGER state communication_owner communication_mode facts_digest focus_event_pk collection_complete:INTEGER thread_cursor thread_poll_at candidate_context_ids_json superseded_by created_at updated_at',
    'delivery_blocks': 'outbox_id case_id lifecycle_round:INTEGER claim_token turn_id turn_revision:INTEGER communication_fence:INTEGER was_delivered:INTEGER notification_outbox_id created_at',
    'diagnostic_snapshots': 'snapshot_id case_id event_pk confidence:REAL created_at input_digest source_digest',
    'incident_cluster_members': 'cluster_id case_id similarity:REAL created_at',
    'job_attempts': 'attempt_id job_id attempt_no:INTEGER started_at ended_at worker_id',
    'jobs': 'job_id case_id job_type state priority:INTEGER lease_owner lease_expires_at heartbeat_at pid:INTEGER process_start_token session_id workdir input_digest output_digest exit_code:INTEGER error_class attempt_no:INTEGER max_attempts:INTEGER available_at created_at updated_at lifecycle_round:INTEGER',
    'knowledge_feedback': 'feedback_id knowledge_id case_id actor_id verdict created_at',
    'locks': 'lock_key owner case_id scope acquired_at expires_at heartbeat_at',
    'mail_digest_links': 'digest_id message_id ordinal:INTEGER share_outbox_id resolve_outbox_id im_message_id state created_at updated_at',
    'mail_digest_runs': 'digest_id summary_type watermark_key range_start range_end item_count:INTEGER content_digest telegram_destination state telegram_outbox_id created_at delivered_at membership_digest timezone notification_channel',
    'mail_items': 'message_id thread_id mailbox sender_name sender_address folder_id label_ids_json internal_date classification confidence:REAL deadline notified:INTEGER case_id received_at updated_at',
    'mail_summary_membership': 'digest_id message_id ordinal:INTEGER category attention thread_id received_epoch_ms:INTEGER',
    'meeting_create_attempts': 'attempt_id preview_id case_id lifecycle_round:INTEGER action_digest phase dispatch_token global_fence:INTEGER invitation_dispatched_at event_id successor_preview_id revision:INTEGER created_at updated_at',
    'meeting_previews': 'preview_id case_id action_digest status approval_id calendar_event_id created_at updated_at',
    'model_budget_attempts': 'attempt_id request_id request_digest case_id budget_day provider model currency policy_revision:INTEGER reserved:INTEGER charged:INTEGER state receipt_id created_at updated_at',
    'operator_activities': 'activity_id external_id activity_type signal action message_id chat_id thread_id root_message_id reply_to_message_id matched_turn_id actor_id occurred_at created_at',
    'professional_validation_runs': 'validation_id revision_id claim_id layer result artifact_digest case_id observed_at',
    'route_decisions': 'route_decision_id event_pk case_id route proposed_route confidence:REAL issue_type severity domain reason_codes_json fallback_route requires_owner_judgment:INTEGER model_output_digest review_status reviewed_by reviewed_at created_at conversation_relation conversation_case_id knowledge_id knowledge_source_digest knowledge_match_confidence:REAL',
}
for _table, _declaration in _MORE_METADATA.items():
    METADATA[_table] = tuple(value.split(':')[0] for value in _declaration.split())
    NON_TEXT[_table] = {value.split(':')[0]: value.split(':')[1]
                        for value in _declaration.split() if ':' in value}


def classification(table, field):
    if field in BODY.get(table, ()):
        return 'body_candidate'
    if field in MIXED.get(table, ()):
        return 'mixed_content_and_control'
    if field in METADATA.get(table, ()):
        return 'retained_metadata'
    return 'unclassified'


def inventory_rules(registered):
    fields = [{'table': table, 'field': field, 'classification': classification(table, field),
               'clear_allowed': False}
              for table, columns in sorted(registered.items()) for field in sorted(columns)]
    return {'version': VERSION, 'digest': digest({'version': VERSION, 'body': BODY, 'mixed': MIXED,
                                                'metadata': METADATA, 'non_text': NON_TEXT}),
            'fields': fields, 'registered_fields_classified':
            all(item['classification'] != 'unclassified' for item in fields),
            'whole_schema_classified': False, 'retirement_ready': False}


def core_schema_audit(conn):
    changes = []
    for table, metadata in METADATA.items():
        expected = set(metadata) | set(BODY.get(table, ())) | set(MIXED.get(table, ()))
        actual = {row[1]: row[2] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        for field in sorted(expected | set(actual)):
            kind = ('unknown_column' if field not in expected else 'missing_column' if field not in actual
                    else 'type_changed' if actual[field] != NON_TEXT.get(table, {}).get(field, 'TEXT') else None)
            if kind:
                changes.append({'table': table, 'field': field, 'reason': kind})
    return {'reviewed_tables': sorted(METADATA), 'changes': changes,
            'reviewed_schema_matches': not changes, 'whole_schema_classified': False}
