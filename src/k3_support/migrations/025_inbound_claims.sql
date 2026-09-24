ALTER TABLE inbound_events ADD COLUMN claim_token TEXT;
ALTER TABLE inbound_events ADD COLUMN processing_started_at TEXT;
ALTER TABLE inbound_events ADD COLUMN heartbeat_at TEXT;

-- Stop all old workers before upgrading. A legacy reservation has no proof of
-- ownership and must be claimed anew; terminal events are not replayed.
UPDATE inbound_events SET status='new',lease_owner=NULL,lease_expires_at=NULL,
    processing_started_at=NULL,heartbeat_at=NULL
WHERE status='claimed';

CREATE UNIQUE INDEX idx_inbound_claim_token ON inbound_events(claim_token)
WHERE claim_token IS NOT NULL;
