-- Source-message dependency checks should start at the message, not scan chats.
CREATE INDEX idx_context_focus_source ON conversation_contexts(focus_event_pk,case_id);
CREATE INDEX idx_context_alias_source ON conversation_anchor_aliases(source_event_pk,context_id);
-- Retention includes retired contexts, unlike idx_context_case_round's filter.
CREATE INDEX idx_context_case_retention ON conversation_contexts(case_id,context_id);
