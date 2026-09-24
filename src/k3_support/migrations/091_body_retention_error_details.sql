-- Old receipts covered payload_json only; never retrospectively claim otherwise.
ALTER TABLE body_retention_receipts ADD COLUMN last_error_digest TEXT;
ALTER TABLE body_retention_receipts ADD COLUMN last_error_bytes INTEGER NOT NULL DEFAULT 0;
