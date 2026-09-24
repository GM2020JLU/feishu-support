"""Replayable claims. The high-entropy control key must live outside the worker domain."""

import hashlib
import hmac
import json
from datetime import UTC, datetime

from .broker_catalog import ActiveCatalog
from .broker_claim import claim_next
from .broker_execution_contract import ExecutionContract
from .broker_grants import verify_bound_task
from .broker_policy import execution_allowed
from .broker_protocol import decode_request
from .db import transaction
from .ids import canonical_json, digest


def claim(conn, config, request, *, peer_uid, control_key, now=None, contract_reader=None):
    if not isinstance(control_key, bytes) or len(control_key) != 32:
        raise ValueError("32-byte control key required")
    request = decode_request(canonical_json(request).encode())
    if request["method"] != "claim" or request["params"]["pool"] != "debug":
        raise ValueError("unsupported claim pool")
    if type(peer_uid) is not int or not 0 < peer_uid < 4294967295:
        raise ValueError("invalid peer UID")
    now = now or datetime.now(UTC)
    if type(contract_reader) is ActiveCatalog:
        contract_reader.check_claim(request["request_id"])
    contract = contract_reader() if contract_reader else None
    request_digest = digest(request)
    key_digest = hashlib.sha256(control_key).hexdigest()
    token = hmac.new(control_key, canonical_json(
        ["k3-support/broker-claim/v1", peer_uid, request["request_id"], request_digest]
    ).encode(), hashlib.sha256).hexdigest()
    with transaction(conn):
        if not execution_allowed(conn, config, contract=contract):
            raise ValueError("claim disabled")
        launch = conn.execute("SELECT * FROM broker_launch_bindings WHERE claim_request_id=?",
                              (request["request_id"],)).fetchone()
        if launch is not None:
            if (type(contract) is not ExecutionContract or contract.agent != launch["agent"]
                    or contract.fingerprint != launch["contract_fingerprint"]):
                raise ValueError("launch contract changed")
            job = conn.execute("SELECT input_digest FROM jobs WHERE job_id=?", (launch["job_id"],)).fetchone()
            if job is None or job["input_digest"] != launch["input_digest"]:
                raise ValueError("launch task changed")
        prior = conn.execute("SELECT * FROM broker_claim_receipts WHERE peer_uid=? AND request_id=?",
                             (peer_uid, request["request_id"])).fetchone()
        if prior is not None:
            if prior["request_digest"] != request_digest or prior["key_digest"] != key_digest:
                raise ValueError("claim receipt binding changed")
            binding = json.loads(prior["binding_json"])
            if binding is None:
                return {"task": None}
            if launch is not None and (binding["job_id"] != launch["job_id"]
                                       or binding["input_digest"] != launch["input_digest"]):
                raise ValueError("claim differs from launch binding")
            task = {**binding, "lease_token": token}
            verify_bound_task(conn, task, peer_uid=peer_uid, now=now)
            return {"task": task}
        task = claim_next(conn, config, worker_uid=peer_uid, now=now, _token=token, contract=contract,
                          job_id=launch["job_id"] if launch is not None else None)
        binding = {key: value for key, value in task.items() if key != "lease_token"} if task else None
        conn.execute("INSERT INTO broker_claim_receipts VALUES(?,?,?,?,?,?)",
                     (peer_uid, request["request_id"], request_digest, key_digest,
                      canonical_json(binding), now.isoformat()))
        return {"task": task}
