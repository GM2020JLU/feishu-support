ALTER TABLE notification_snooze ADD COLUMN night_enabled INTEGER NOT NULL DEFAULT 0 CHECK(night_enabled IN (0,1));
ALTER TABLE notification_snooze_history ADD COLUMN night_enabled INTEGER NOT NULL DEFAULT 0 CHECK(night_enabled IN (0,1));
