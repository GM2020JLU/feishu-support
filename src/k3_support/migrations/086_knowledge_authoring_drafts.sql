CREATE TABLE knowledge_authoring_drafts (
    candidate_id TEXT PRIMARY KEY,
    revision_digest TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
    markdown TEXT NOT NULL,
    material_json TEXT NOT NULL CHECK(json_valid(material_json)),
    saved_by TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
