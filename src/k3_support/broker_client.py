"""Restricted worker-side exchange, without database or execution imports."""

import math
import socket
import time

from .broker_identity import authenticate_worker
from .broker_protocol import ProtocolError, decode_response, encode_request
from .broker_transport import receive_frame


def request_at(socket_path, request, *, control_uid, timeout=5.0):
    """Connect to one configured filesystem Unix socket, with no retry or fallback."""
    if (not isinstance(socket_path, str) or not socket_path.startswith("/")
            or "\0" in socket_path or len(socket_path.encode()) > 107):
        raise ProtocolError("absolute Unix socket path required")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ProtocolError("invalid exchange timeout")
    encode_request(request)  # Reject invalid work before opening a connection.
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(socket_path)
        except OSError as error:
            raise ProtocolError("broker connection unavailable") from error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError("broker connection deadline exceeded")
        return exchange(sock, request, control_uid=control_uid, timeout=remaining)


def exchange(sock, request, *, control_uid, timeout=5.0):
    """Own a connected socket. Authenticate server before sending secrets; never retry.

    A transport failure after send has unknown delivery. Preserve the request ID
    for an explicit replay, not a newly generated request.
    """
    with sock:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ProtocolError("invalid exchange timeout")
        authenticate_worker(sock, worker_uid=control_uid)
        frame = encode_request(request)
        deadline = time.monotonic() + timeout
        sock.settimeout(timeout)
        try:
            sock.sendall(frame)
        except OSError as error:
            raise ProtocolError("request delivery unknown") from error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError("response deadline exceeded; delivery unknown")
        raw = receive_frame(sock, timeout=remaining)
        return decode_response(raw, request_id=request["request_id"])
