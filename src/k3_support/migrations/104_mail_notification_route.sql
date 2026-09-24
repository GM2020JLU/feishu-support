-- Historical destination/outbox column names are retained for compatibility.
ALTER TABLE mail_digest_runs ADD COLUMN notification_channel TEXT NOT NULL DEFAULT 'telegram'
    CHECK(notification_channel IN ('telegram','feishu_im','web'));
CREATE TRIGGER mail_digest_route_immutable
BEFORE UPDATE OF notification_channel,telegram_destination ON mail_digest_runs
WHEN NEW.notification_channel != OLD.notification_channel
  OR NEW.telegram_destination != OLD.telegram_destination
BEGIN
    SELECT RAISE(ABORT, 'mail digest notification route is immutable');
END;
