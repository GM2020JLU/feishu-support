"""Bounded serial broker loop for a deployment-owned Unix listener."""

import math
import os
import socket
import time

from .broker_connection import serve_connection
from .broker_identity import IdentityError
from .broker_protocol import ProtocolError


def serve(conn, listener, *, config, worker_uid, control_key, stop_event,
          request_timeout=5.0, max_connections=None, unit_references=None, contract_reader=None):
    """Borrow listener; close accepted sockets. Never bind/unlink deployment paths.

    A stop request prevents the next accept; an in-flight request has a bounded
    transport deadline but is not proof that a coding process has stopped.
    """
    if (type(worker_uid) is not int or not 0 < worker_uid < 4294967295
            or worker_uid == os.geteuid()):
        raise ValueError("independent worker UID required")
    if not isinstance(control_key, bytes) or len(control_key) != 32:
        raise ValueError("32-byte control key required")
    if (type(request_timeout) not in (int, float) or not math.isfinite(request_timeout)
            or not 0 < request_timeout <= 30):
        raise ValueError("invalid request timeout")
    if max_connections is not None and (type(max_connections) is not int or max_connections < 1):
        raise ValueError("invalid connection limit")
    if (listener.family != socket.AF_UNIX
            or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
            or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1):
        raise ValueError("listening Unix stream required")
    previous_timeout = listener.gettimeout()
    counts = {"connections": 0, "transport_rejections": 0}
    next_release = time.monotonic() + 5
    try:
        listener.settimeout(0.25)
        while not stop_event.is_set() and (max_connections is None or counts["connections"] < max_connections):
            if unit_references is not None and time.monotonic() >= next_release:
                unit_references.release_recorded(conn)
                next_release = time.monotonic() + 5
            try:
                peer_socket, _ = listener.accept()
            except TimeoutError:
                continue
            if stop_event.is_set():
                peer_socket.close()
                break
            counts["connections"] += 1
            try:
                serve_connection(conn, peer_socket, worker_uid=worker_uid, config=config,
                                 control_key=control_key, timeout=request_timeout,
                                 instance_observer=unit_references.observe if unit_references else None,
                                 contract_reader=contract_reader)
            except (IdentityError, ProtocolError, OSError):
                # Public aggregate only, never exception text or request contents.
                counts["transport_rejections"] += 1
        return counts
    finally:
        listener.settimeout(previous_timeout)
