"""Atomic database-only request receipts; trusted handlers must never perform external I/O."""

import json
from datetime import UTC, datetime

from .broker_grants import verify, verify_bound_task
from .broker_protocol import decode_request, encode_response
from .db import transaction
from .ids import canonical_json, digest


def execute(conn, request, *, peer_uid, handler, now=None, allow_operation=None):
    # Re-validate even for internal callers; raw requests and secrets are never persisted.
    request = decode_request(canonical_json(request).encode())
    if request["method"] not in {"renew", "result", "stop_receipt"}:
        raise ValueError("method has no persistent receipt contract")
    now = now or datetime.now(UTC)
    binding_digest = digest(request)
    with transaction(conn):
        if allow_operation is not None and not allow_operation(conn):
            raise ValueError("operation disabled by current policy")
        grant = verify(conn, token=request["params"]["lease_token"], peer_uid=peer_uid, now=now)
        old = conn.execute("SELECT * FROM broker_receipts WHERE peer_uid=? AND request_id=?",
                           (peer_uid, request["request_id"])).fetchone()
        if old:
            if old["request_digest"] != binding_digest or old["grant_id"] != grant["grant_id"] or old["method"] != request["method"]:
                raise ValueError("request binding changed")
            return json.loads(old["response_json"])
        binding = verify_bound_task(conn, request["params"], peer_uid=peer_uid, now=now)
        result = handler(conn, binding, request)
        # Narrow receipts, not arbitrary task output or capability-bearing claim responses.
        if (not isinstance(result, dict) or set(result) - {"accepted", "job_id", "expires_at"}
                or result.get("accepted") is not True or result.get("job_id") != binding["job_id"]):
            raise ValueError("invalid handler receipt")
        if "expires_at" in result and (not isinstance(result["expires_at"], str) or len(result["expires_at"]) > 64):
            raise ValueError("invalid receipt expiry")
        encode_response(request_id=request["request_id"], result=result)
        conn.execute("INSERT INTO broker_receipts VALUES(?,?,?,?,?,?,?)",
                     (peer_uid, request["request_id"], binding_digest, request["method"],
                      grant["grant_id"], canonical_json(result), now.isoformat()))
    return result
