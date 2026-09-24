-- Bind one authenticated native chat message to one exact creation-control intent.
-- The existing draft and create grant remain the business authority.
CREATE TABLE project_create_chat_intents (
    request_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    draft_id TEXT NOT NULL REFERENCES project_bug_create_drafts(draft_id),
    intent_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER project_create_chat_intent_immutable BEFORE UPDATE ON project_create_chat_intents
BEGIN SELECT RAISE(ABORT,'chat creation intent is immutable'); END;
CREATE TRIGGER project_create_chat_intent_retention BEFORE DELETE ON project_create_chat_intents
BEGIN SELECT RAISE(ABORT,'chat creation intent requires controlled retention'); END;
