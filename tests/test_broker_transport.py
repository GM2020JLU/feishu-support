import json
import socket
import struct
from uuid import uuid4

import pytest

from k3_support.broker_protocol import MAX_REQUEST_BYTES, ProtocolError
from k3_support.broker_transport import receive_request


def test_real_socket_pair_receives_one_request_and_restores_timeout():
    left, right = socket.socketpair()
    with left, right:
        value = {"version": 1, "request_id": str(uuid4()), "method": "claim", "params": {"pool": "debug"}}
        raw = json.dumps(value).encode()
        right.sendall(struct.pack("!I", len(raw)) + raw)
        left.settimeout(2)
        assert receive_request(left) == value
        assert left.gettimeout() == 2


@pytest.mark.parametrize("payload", [b"\x00", struct.pack("!I", 10) + b"{}", struct.pack("!I", 0), struct.pack("!I", MAX_REQUEST_BYTES + 1)])
def test_truncated_and_oversize_frames_fail_closed(payload):
    left, right = socket.socketpair()
    with left, right:
        right.sendall(payload)
        right.shutdown(socket.SHUT_WR)
        with pytest.raises(ProtocolError):
            receive_request(left)
        assert left.gettimeout() is None


def test_stalled_socket_has_bounded_wait():
    left, right = socket.socketpair()
    with left, right:
        with pytest.raises(ProtocolError, match="transport|deadline"):
            receive_request(left, timeout=0.01)
        assert left.gettimeout() is None


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan"), 31])
def test_invalid_deadline_is_rejected(timeout):
    with pytest.raises(ProtocolError, match="timeout"):
        receive_request(None, timeout=timeout)
