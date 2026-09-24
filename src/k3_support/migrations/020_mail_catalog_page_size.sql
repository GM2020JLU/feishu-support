ALTER TABLE mail_catalog_runs
ADD COLUMN page_size INTEGER NOT NULL DEFAULT 100 CHECK(page_size >= 1 AND page_size <= 100);
