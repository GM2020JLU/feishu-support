-- Content versioning is not publication or model-reply authorization.
CREATE TABLE knowledge_corpus_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    revision INTEGER NOT NULL CHECK(revision>=1),
    built_revision INTEGER,
    corpus_digest TEXT,
    sources_digest TEXT
);
INSERT INTO knowledge_corpus_state(singleton,revision) VALUES(1,1);
CREATE TABLE knowledge_corpus_builds (
    revision INTEGER PRIMARY KEY,
    corpus_digest TEXT NOT NULL,
    sources_digest TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    built_at TEXT NOT NULL
);
CREATE TABLE knowledge_corpus_metadata (
    revision INTEGER NOT NULL REFERENCES knowledge_corpus_builds(revision),
    knowledge_id TEXT NOT NULL,
    entry_digest TEXT NOT NULL,
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
    PRIMARY KEY(revision,knowledge_id)
);
CREATE TABLE knowledge_corpus_terms (
    revision INTEGER NOT NULL REFERENCES knowledge_corpus_builds(revision),
    token TEXT NOT NULL,
    knowledge_id TEXT NOT NULL,
    weight INTEGER NOT NULL CHECK(weight IN (1,4)),
    PRIMARY KEY(revision,token,knowledge_id)
);
CREATE TRIGGER corpus_change_knowledge_entries_insert
AFTER INSERT ON knowledge_entries
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_knowledge_entries_update
AFTER UPDATE OF knowledge_id,title,status,question_variants_json,answer_markdown,project,module,hardware,software_version,applicability,disclosure_class,allowed_chat_ids_json,allowed_user_ids_json,confidence,source_authority,review_due_at,source_digest,content_digest,professional_revision_id ON knowledge_entries
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_knowledge_entries_delete
AFTER DELETE ON knowledge_entries
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_knowledge_sources_insert
AFTER INSERT ON knowledge_sources
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_knowledge_sources_update
AFTER UPDATE ON knowledge_sources
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_knowledge_sources_delete
AFTER DELETE ON knowledge_sources
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_source_registry_insert
AFTER INSERT ON source_registry
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_source_registry_update
AFTER UPDATE ON source_registry
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_source_registry_delete
AFTER DELETE ON source_registry
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_knowledge_revisions_insert
AFTER INSERT ON professional_knowledge_revisions
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_knowledge_revisions_update
AFTER UPDATE ON professional_knowledge_revisions
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_knowledge_revisions_delete
AFTER DELETE ON professional_knowledge_revisions
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_knowledge_claims_insert
AFTER INSERT ON professional_knowledge_claims
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_knowledge_claims_update
AFTER UPDATE ON professional_knowledge_claims
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_knowledge_claims_delete
AFTER DELETE ON professional_knowledge_claims
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_claim_sources_insert
AFTER INSERT ON professional_claim_sources
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_claim_sources_update
AFTER UPDATE ON professional_claim_sources
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_claim_sources_delete
AFTER DELETE ON professional_claim_sources
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_validation_runs_insert
AFTER INSERT ON professional_validation_runs
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_validation_runs_update
AFTER UPDATE ON professional_validation_runs
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_validation_runs_delete
AFTER DELETE ON professional_validation_runs
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_knowledge_publications_insert
AFTER INSERT ON professional_knowledge_publications
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_knowledge_publications_update
AFTER UPDATE ON professional_knowledge_publications
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_knowledge_publications_delete
AFTER DELETE ON professional_knowledge_publications
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_source_change_events_insert
AFTER INSERT ON professional_source_change_events
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_source_change_events_update
AFTER UPDATE ON professional_source_change_events
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
CREATE TRIGGER corpus_change_professional_source_change_events_delete
AFTER DELETE ON professional_source_change_events
BEGIN
    UPDATE knowledge_corpus_state SET revision=revision+1 WHERE singleton=1;
END;
