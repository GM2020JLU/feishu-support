import json
from uuid import uuid4

import pytest

from k3_support.broker_protocol import MAX_REQUEST_BYTES, ProtocolError, decode_request


def request(method="claim", params=None):
    return {"version": 1, "request_id": str(uuid4()), "method": method, "params": params or {"pool": "debug"}}


def test_claim_round_trip_and_unknown_authority_rejected():
    value = request()
    assert decode_request(json.dumps(value).encode()) == value
    for key in ("uid", "owner", "role", "command", "sql"):
        with pytest.raises(ProtocolError):
            decode_request(json.dumps({**value, key: "injected"}).encode())


@pytest.mark.parametrize("raw", [b"", b"x" * (MAX_REQUEST_BYTES + 1), b"\xff", b'{"x":1,"x":2}', b'{"x":NaN}', b'[]', b'[' * 2000 + b']' * 2000])
def test_malformed_input_fails_closed(raw):
    with pytest.raises(ProtocolError):
        decode_request(raw)


@pytest.mark.parametrize("method", ["approve", "shell", "sql", "send", "push", "board"])
def test_arbitrary_capabilities_are_not_protocol_methods(method):
    with pytest.raises(ProtocolError):
        decode_request(json.dumps(request(method)).encode())


def test_task_requires_full_binding_and_strict_rounds():
    binding = {"job_id": "job-1", "case_id": "CASE-1", "lifecycle_round": 1, "execution_round": 1,
               "lease_token": "token", "input_digest": "a" * 64}
    value = request("renew", binding)
    assert decode_request(json.dumps(value).encode()) == value
    for key in binding:
        broken = dict(binding)
        del broken[key]
        with pytest.raises(ProtocolError):
            decode_request(json.dumps(request("renew", broken)).encode())
    with pytest.raises(ProtocolError):
        decode_request(json.dumps(request("renew", {**binding, "execution_round": True})).encode())


@pytest.mark.parametrize("version", [True, "1", 0, 2, None])
def test_version_is_exact_integer(version):
    with pytest.raises(ProtocolError):
        decode_request(json.dumps({**request(), "version": version}).encode())


def test_nested_duplicate_parameter_rejected():
    raw = json.dumps(request()).replace('"pool": "debug"', '"pool":"build","pool":"debug"')
    with pytest.raises(ProtocolError):
        decode_request(raw.encode())


@pytest.mark.parametrize("method,extra", [("result", {"result": "synthetic report"}), ("stop_receipt", {"exit_code": -15}), ("input", {})])
def test_other_methods_require_binding_and_do_not_accept_authority_fields(method, extra):
    params = {"job_id": "job-1", "case_id": "CASE-1", "lifecycle_round": 1, "execution_round": 1,
              "lease_token": "token", "input_digest": "a" * 64, **extra}
    value = request(method, params)
    assert decode_request(json.dumps(value).encode()) == value
    with pytest.raises(ProtocolError):
        decode_request(json.dumps(request(method, {**params, "approved": True})).encode())


@pytest.mark.parametrize("method,extra", [("result", {"result": {}}), ("result", {"result": "x" * 200001}),
                                         ("stop_receipt", {"exit_code": True}), ("stop_receipt", {"exit_code": 256})])
def test_invalid_result_or_exit_receipt(method, extra):
    params = {"job_id": "job-1", "case_id": "CASE-1", "lifecycle_round": 1, "execution_round": 1,
              "lease_token": "token", "input_digest": "a" * 64, **extra}
    with pytest.raises(ProtocolError):
        decode_request(json.dumps(request(method, params)).encode())
