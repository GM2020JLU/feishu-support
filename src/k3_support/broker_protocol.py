"""Version-one request decoding. Parsing grants no authority and performs no I/O."""

import json
import re
import struct
from uuid import UUID

MAX_REQUEST_BYTES = 262144
METHODS = {"claim", "input", "start", "renew", "result", "stop_receipt", "remote_submit", "remote_read", "board_submit", "board_read", "verification_list"}
ERROR_CODES = {"invalid_request", "unauthorized", "stale_binding", "conflict", "unavailable"}


class ProtocolError(ValueError):
    pass


def encode_request(request):
    try:
        raw = json.dumps(request, ensure_ascii=True, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ProtocolError("invalid request JSON") from error
    decode_request(raw)
    return struct.pack("!I", len(raw)) + raw


def decode_response(raw, *, request_id):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_REQUEST_BYTES:
        raise ProtocolError("invalid response size")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ProtocolError("invalid response JSON") from error
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or value["version"] != 1 or value.get("request_id") != request_id
            or type(value.get("ok")) is not bool):
        raise ProtocolError("invalid response binding")
    field = "result" if value["ok"] else "error"
    if set(value) != {"version", "request_id", "ok", field}:
        raise ProtocolError("invalid response fields")
    # Reuse the canonical encoder's UUID, result and error-code constraints.
    encode_response(request_id=request_id, result=value.get("result"), error_code=value.get("error"))
    return value


def encode_response(*, request_id, result=None, error_code=None) -> bytes:
    """Frame a bounded response; errors carry only an allowlisted public code."""
    try:
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError()
    except ValueError as error:
        raise ProtocolError("invalid response request ID") from error
    value = {"version": 1, "request_id": request_id}
    if error_code is not None:
        if not isinstance(error_code, str) or error_code not in ERROR_CODES or result is not None:
            raise ProtocolError("invalid response error")
        value.update(ok=False, error=error_code)
    else:
        if not isinstance(result, dict):
            raise ProtocolError("response result must be an object")
        value.update(ok=True, result=result)
    try:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ProtocolError("invalid response JSON") from error
    if len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError("response too large")
    return struct.pack("!I", len(raw)) + raw


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("duplicate field")
        value[key] = item
    return value


def _constant(_value):
    raise ProtocolError("non-finite number")


def decode_request(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_REQUEST_BYTES:
        raise ProtocolError("invalid request size")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ProtocolError("invalid request JSON") from error
    if not isinstance(value, dict) or set(value) != {"version", "request_id", "method", "params"}:
        raise ProtocolError("invalid envelope")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ProtocolError("unsupported version")
    request_id = value["request_id"]
    try:
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError()
    except ValueError as error:
        raise ProtocolError("invalid request ID") from error
    method, params = value["method"], value["params"]
    if not isinstance(method, str) or method not in METHODS or not isinstance(params, dict):
        raise ProtocolError("invalid method or params")
    if method == "claim":
        if set(params) != {"pool"} or params["pool"] not in ("debug", "build"):
            raise ProtocolError("invalid claim")
        return value
    fields = {"job_id", "case_id", "lifecycle_round", "execution_round", "lease_token", "input_digest"}
    if method == "verification_list":
        fields |= {"after_id"}
        if not isinstance(params.get("after_id"), str) or len(params["after_id"]) > 256:
            raise ProtocolError("invalid verification cursor")
    if method == "result":
        fields |= {"result"}
    if method in {"remote_read", "board_read"}:
        target_key = "remote_request_id" if method == "remote_read" else "board_request_id"
        fields |= {target_key, "offset"}
        target = params.get(target_key)
        try:
            if not isinstance(target, str) or str(UUID(target)) != target:
                raise ValueError()
        except ValueError as error:
            raise ProtocolError("invalid remote request ID") from error
        if type(params.get("offset")) is not int or not 0 <= params["offset"] <= 128000:
            raise ProtocolError("invalid remote output offset")
    if method == "remote_submit":
        fields |= {"remote"}
        remote = params.get("remote")
        if (not isinstance(remote, dict) or set(remote) != {"mode", "repo", "command"}
                or remote["mode"] not in ("inspect", "work")
                or not isinstance(remote["command"], str) or not remote["command"]
                or "\x00" in remote["command"] or re.search(r"[\ud800-\udfff]", remote["command"])
                or len(remote["command"].encode("utf-8")) > 32768
                or (remote["mode"] == "inspect" and remote["repo"] is not None)
                or (remote["mode"] == "work" and remote["repo"] is None)
                or (remote["repo"] is not None and (not isinstance(remote["repo"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", remote["repo"])))):
            raise ProtocolError("invalid remote task")
    if method == "board_submit":
        fields |= {"session_id", "action"}
        if not isinstance(params.get("session_id"), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", params["session_id"]):
            raise ProtocolError("invalid board session")
        from .executors import ExecutorError, validate_board_action
        try:
            action = validate_board_action(params.get("action"))
            encoded = json.dumps(action, ensure_ascii=False).encode("utf-8")
            if len(encoded) > 70000 or any(isinstance(v, str) and "\x00" in v for v in action.values()):
                raise ValueError("invalid board action encoding")
            if "timeout" in action and type(action["timeout"]) is not int:
                raise ValueError("integer board timeout required")
        except (ExecutorError, ValueError, UnicodeError) as error:
            raise ProtocolError("invalid board action") from error
    if method == "stop_receipt":
        fields |= {"exit_code"}
    if method == "start" and "contract_fingerprint" in params:
        fields |= {"contract_fingerprint"}
        if not isinstance(params["contract_fingerprint"], str) or not re.fullmatch(r"[a-f0-9]{64}", params["contract_fingerprint"]):
            raise ProtocolError("invalid execution contract fingerprint")
    if set(params) != fields:
        raise ProtocolError("invalid task binding fields")
    for key in ("job_id", "case_id", "lease_token"):
        item = params[key]
        if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", item):
            raise ProtocolError("invalid task identifier")
    for key in ("lifecycle_round", "execution_round"):
        if type(params[key]) is not int or not 1 <= params[key] <= 2147483647:
            raise ProtocolError("invalid task round")
    if not isinstance(params["input_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", params["input_digest"]):
        raise ProtocolError("invalid input digest")
    if method == "result" and (not isinstance(params["result"], str) or len(params["result"]) > 200000):
        raise ProtocolError("invalid result")
    if method == "stop_receipt" and (type(params["exit_code"]) is not int or not -255 <= params["exit_code"] <= 255):
        raise ProtocolError("invalid exit code")
    return value
