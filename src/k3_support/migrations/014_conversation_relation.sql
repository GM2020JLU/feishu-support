ALTER TABLE route_decisions
    ADD COLUMN conversation_relation TEXT NOT NULL DEFAULT 'standalone'
    CHECK(conversation_relation IN ('standalone','continuation','acknowledgement','new_topic'));

ALTER TABLE route_decisions
    ADD COLUMN conversation_case_id TEXT REFERENCES cases(case_id);

ALTER TABLE route_decisions
    ADD COLUMN knowledge_id TEXT REFERENCES knowledge_entries(knowledge_id);

ALTER TABLE route_decisions
    ADD COLUMN knowledge_source_digest TEXT;

ALTER TABLE route_decisions
    ADD COLUMN knowledge_match_confidence REAL
    CHECK(knowledge_match_confidence IS NULL OR
          (knowledge_match_confidence >= 0 AND knowledge_match_confidence <= 1));
