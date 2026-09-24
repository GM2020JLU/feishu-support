ALTER TABLE mail_catalog_runs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE mail_catalog_runs ADD COLUMN error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json));
ALTER TABLE mail_catalog_runs ADD COLUMN page_limit INTEGER NOT NULL DEFAULT 10000 CHECK(page_limit>0);
CREATE TABLE mail_catalog_cursors (
    run_id TEXT NOT NULL REFERENCES mail_catalog_runs(run_id),
    folder_index INTEGER NOT NULL,
    token_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id,folder_index,token_digest)
);
