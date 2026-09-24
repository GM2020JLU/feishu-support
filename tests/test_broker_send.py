import json
import socket
import struct
from uuid import uuid4

import pytest

from k3_support.broker_protocol import ProtocolError
from k3_support.broker_transport import send_response


def test_response_crosses_real_socket_and_restores_timeout():
    left, right = socket.socketpair()
    with left, right:
        left.settimeout(2)
        request_id = str(uuid4())
        send_response(left, request_id=request_id, result={"accepted": True})
        right.settimeout(1)
        length = struct.unpack("!I", right.recv(4))[0]
        raw = bytearray()
        while len(raw) < length:
            raw.extend(right.recv(length - len(raw)))
        assert json.loads(raw)["request_id"] == request_id
        assert left.gettimeout() == 2


def test_nonreading_client_is_bounded_and_delivery_unknown():
    left, right = socket.socketpair()
    with left, right:
        left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        with pytest.raises(ProtocolError, match="delivery unknown"):
            send_response(left, request_id=str(uuid4()), result={"body": "x" * 200000}, timeout=0.01)
        assert left.gettimeout() is None


def test_disconnected_client_is_not_retried():
    left, right = socket.socketpair()
    right.close()
    with left:
        with pytest.raises(ProtocolError, match="delivery unknown"):
            send_response(left, request_id=str(uuid4()), result={})
        assert left.gettimeout() is None
