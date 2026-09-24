CREATE TABLE broker_receipts (
    peer_uid INTEGER NOT NULL CHECK(peer_uid>0 AND peer_uid<4294967295),
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
    method TEXT NOT NULL CHECK(method IN ('renew','result','stop_receipt')),
    grant_id TEXT NOT NULL REFERENCES broker_grants(grant_id),
    response_json TEXT NOT NULL CHECK(json_valid(response_json) AND length(response_json)<=262144),
    created_at TEXT NOT NULL,
    PRIMARY KEY(peer_uid,request_id)
);
