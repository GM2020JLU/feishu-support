import json
import struct
from uuid import uuid4

import pytest

from k3_support.broker_protocol import ProtocolError, encode_response


def test_response_frame_binds_request_and_hides_error_details():
    request_id = str(uuid4())
    frame = encode_response(request_id=request_id, result={"job_id": "job-1"})
    assert struct.unpack("!I", frame[:4])[0] == len(frame) - 4
    assert json.loads(frame[4:]) == {"version": 1, "request_id": request_id, "ok": True, "result": {"job_id": "job-1"}}
    error = json.loads(encode_response(request_id=request_id, error_code="unauthorized")[4:])
    assert set(error) == {"version", "request_id", "ok", "error"}
    assert error["ok"] is False


@pytest.mark.parametrize("result", [None, [], {"bad": float("nan")}, {"bad": object()}, {"body": "x" * 262144}])
def test_invalid_or_oversize_results_are_rejected(result):
    with pytest.raises(ProtocolError):
        encode_response(request_id=str(uuid4()), result=result)


def test_unknown_errors_and_success_error_mixture_are_rejected():
    with pytest.raises(ProtocolError):
        encode_response(request_id=str(uuid4()), error_code="database password=secret")
    with pytest.raises(ProtocolError):
        encode_response(request_id=str(uuid4()), result={}, error_code="conflict")
