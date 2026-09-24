"""One bounded request per connection; no listener, identity grant or dispatcher."""

import math
import struct
import time

from .broker_protocol import (
    MAX_REQUEST_BYTES,
    ProtocolError,
    decode_request,
    encode_response,
)


def send_response(sock, *, request_id, result=None, error_code=None, timeout=5.0):
    """Send once with a total deadline; failure never implies safe action replay."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ProtocolError("invalid response timeout")
    frame = encode_response(request_id=request_id, result=result, error_code=error_code)
    previous = sock.gettimeout()
    try:
        sock.settimeout(timeout)
        sock.sendall(frame)
    except OSError as error:
        raise ProtocolError("response transport failed; delivery unknown") from error
    finally:
        sock.settimeout(previous)


def receive_authenticated_request(sock, *, worker_uid, timeout=5.0):
    """Authenticate before reading any untrusted frame; task authorization is separate."""
    from .broker_identity import authenticate_worker

    peer = authenticate_worker(sock, worker_uid=worker_uid)
    return peer, receive_request(sock, timeout=timeout)


def receive_request(sock, *, timeout=5.0):
    return decode_request(receive_frame(sock, timeout=timeout))


def receive_frame(sock, *, timeout=5.0):
    """Read one bounded length-prefixed payload; caller validates its schema."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ProtocolError("invalid request timeout")
    previous = sock.gettimeout()
    deadline = time.monotonic() + timeout

    def exact(size):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError("request deadline exceeded")
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(size - len(data))
            except (TimeoutError, OSError) as error:
                raise ProtocolError("request transport failed") from error
            if not chunk:
                raise ProtocolError("truncated request")
            data.extend(chunk)
        return bytes(data)

    try:
        length = struct.unpack("!I", exact(4))[0]
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise ProtocolError("invalid frame size")
        return exact(length)
    finally:
        sock.settimeout(previous)
