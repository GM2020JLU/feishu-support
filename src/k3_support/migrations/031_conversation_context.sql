CREATE TABLE conversation_contexts (
    context_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    chat_type TEXT NOT NULL CHECK(chat_type IN ('p2p','group')),
    requester_id TEXT,
    case_id TEXT REFERENCES cases(case_id),
    lifecycle_round INTEGER,
    revision INTEGER NOT NULL DEFAULT 0,
    input_digest TEXT NOT NULL,
    projected_revision INTEGER NOT NULL DEFAULT -1,
    state TEXT NOT NULL DEFAULT 'dirty' CHECK(state IN ('dirty','ready','incomplete','conflict','awaiting_relation','retired')),
    communication_owner TEXT NOT NULL DEFAULT 'ai' CHECK(communication_owner IN ('ai','human')),
    communication_mode TEXT NOT NULL DEFAULT 'respond' CHECK(communication_mode IN ('respond','silent','suggest_only')),
    query_text TEXT NOT NULL DEFAULT '',
    facts_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(facts_json)),
    facts_digest TEXT,
    focus_event_pk TEXT REFERENCES inbound_events(event_pk),
    collection_complete INTEGER NOT NULL DEFAULT 1 CHECK(collection_complete IN (0,1)),
    thread_cursor TEXT,
    thread_poll_at TEXT,
    pending_associations_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(pending_associations_json)),
    candidate_context_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(candidate_context_ids_json)),
    conflicts_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(conflicts_json)),
    superseded_by TEXT REFERENCES conversation_contexts(context_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_context_case_round ON conversation_contexts(case_id,lifecycle_round)
    WHERE case_id IS NOT NULL AND state<>'retired';
CREATE INDEX idx_context_chat ON conversation_contexts(chat_id,chat_type,requester_id,state);

CREATE TABLE conversation_anchor_aliases (
    chat_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    context_id TEXT NOT NULL REFERENCES conversation_contexts(context_id),
    source_event_pk TEXT REFERENCES inbound_events(event_pk),
    PRIMARY KEY(chat_id,alias)
);
CREATE INDEX idx_context_alias ON conversation_anchor_aliases(context_id);

CREATE TABLE conversation_context_members (
    event_pk TEXT PRIMARY KEY REFERENCES inbound_events(event_pk),
    context_id TEXT NOT NULL REFERENCES conversation_contexts(context_id),
    member_sequence INTEGER NOT NULL,
    event_digest TEXT NOT NULL,
    canonical_digest TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('colleague','owner')),
    relation TEXT NOT NULL,
    admitted_at TEXT NOT NULL,
    UNIQUE(context_id,member_sequence)
);
CREATE INDEX idx_context_member ON conversation_context_members(context_id,member_sequence);

ALTER TABLE outbox ADD COLUMN context_id TEXT REFERENCES conversation_contexts(context_id);
ALTER TABLE outbox ADD COLUMN context_revision INTEGER;
ALTER TABLE outbox ADD COLUMN context_digest TEXT;
CREATE INDEX idx_outbox_context ON outbox(context_id,context_revision,state);
