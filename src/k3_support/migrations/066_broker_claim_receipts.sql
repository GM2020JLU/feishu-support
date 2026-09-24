CREATE TABLE broker_claim_receipts (
    peer_uid INTEGER NOT NULL CHECK(peer_uid>0 AND peer_uid<4294967295),
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    key_digest TEXT NOT NULL,
    binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(peer_uid,request_id)
);
