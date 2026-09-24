-- Legacy decisions have no attested retrieval provenance. Never guess it on replay.
ALTER TABLE route_decisions ADD COLUMN knowledge_runtime_json TEXT NOT NULL
DEFAULT '{}' CHECK(json_valid(knowledge_runtime_json));
