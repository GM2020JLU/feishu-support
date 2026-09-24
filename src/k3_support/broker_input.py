"""Authorized immutable task-input projection; no filesystem or context fallback."""

import json
import re

from .broker_grants import verify_bound_task
from .broker_protocol import decode_request, encode_response
from .db import transaction
from .ids import canonical_json, digest


def project(conn, *, job_id, case_id, lifecycle_round, input_digest, request_id):
    """Trusted transaction caller supplies a binding; this is not authorization."""
    if not conn.in_transaction:
        raise ValueError("control transaction required")
    from .content_retirement import require_case_content
    require_case_content(conn, case_id=case_id, lifecycle_round=lifecycle_round)
    row = conn.execute("SELECT payload_json FROM broker_inputs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("immutable input unavailable")
    payload = json.loads(row["payload_json"])
    fields = {"case_id", "lifecycle_round", "brief", "repos", "model", "reasoning", "context_extra"}
    if (not isinstance(payload, dict) or set(payload) != fields or digest(payload) != input_digest
            or payload["case_id"] != case_id or type(payload["lifecycle_round"]) is not int
            or payload["lifecycle_round"] != lifecycle_round
            or any(not isinstance(payload[key], str) or not payload[key] for key in ("brief", "model", "reasoning"))
            or not isinstance(payload["repos"], list) or not payload["repos"]
            or any(not isinstance(repo, str) or not repo for repo in payload["repos"])
            or not isinstance(payload["context_extra"], dict)):
        raise ValueError("input snapshot binding changed")
    result = {"job_id": job_id, "input_digest": input_digest,
              **{key: payload[key] for key in ("brief", "repos", "model", "reasoning")}}
    if "execution" in payload["context_extra"]:
        from .broker_execution_contract import validate_selection
        result["execution"] = validate_selection(payload["context_extra"]["execution"])
    if "project_investigation" in payload["context_extra"]:
        from .project_investigation_source import validate

        context = payload["context_extra"]["project_investigation"]
        if not isinstance(context, dict) or "source" not in context:
            raise ValueError("investigation source binding unavailable")
        result["investigation_source"] = validate(context["source"])
        if "predecessor_job_id" in context:
            from .project_investigation import require_predecessor

            require_predecessor(conn, context)
        if "verification" in context:
            from .project_verifier_job import selection
            from .project_verifier_repository_set import source_map

            verification = selection(context["verification"])
            sources = source_map(verification, result["investigation_source"], payload["repos"][0])
            if set(payload["repos"]) != set(sources):
                raise ValueError("verification repositories differ from the immutable source set")
            expected_order = [payload["repos"][0]] + sorted(set(sources) - {payload["repos"][0]})
            if payload["repos"] != expected_order:
                raise ValueError("verification repositories must keep the primary repository first")
            if len(sources) > 1:
                result["investigation_sources"] = sources
            result["verification"] = verification
        elif len(payload["repos"]) != 1:
            raise ValueError("multi-repository investigation requires a joint verifier")
    if "board_session_id" in payload["context_extra"]:
        session = payload["context_extra"]["board_session_id"]
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", session):
            raise ValueError("invalid immutable board session")
        result["board_session_id"] = session
    encode_response(request_id=request_id, result=result)
    return result


def read(conn, request, *, peer_uid, now=None):
    request = decode_request(canonical_json(request).encode())
    if request["method"] != "input":
        raise ValueError("input request required")
    with transaction(conn):
        binding = verify_bound_task(conn, request["params"], peer_uid=peer_uid, now=now)
        return project(conn, job_id=binding["job_id"], case_id=request["params"]["case_id"],
                       lifecycle_round=binding["lifecycle_round"], input_digest=binding["input_digest"],
                       request_id=request["request_id"])
