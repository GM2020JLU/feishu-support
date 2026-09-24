"""Minimal privileged launch protocol: exactly one UUID, no unit properties."""

import os
import socket
import stat
import struct
import time
from uuid import UUID

from .broker_dispatch import start_unit


def serve_connection(sock, *, control_uid, state_fd, launch=start_unit):
    """Borrow a root-owned private state directory; own the accepted socket.

    A durable O_EXCL intent precedes launch. Crashes/unknown results never replay.
    The deployment must keep this helper, its interpreter and all ancestors root
    owned. This function's injectable launcher is only for local synthetic tests.
    """
    with sock:
        if type(control_uid) is not int or control_uid <= 0:
            raise ValueError("independent control UID required")
        _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != control_uid:
            raise ValueError("unauthorized launcher peer")
        directory = os.fstat(state_fd)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != 0 or directory.st_mode & 0o077:
            raise ValueError("private root-owned launcher state required")
        deadline = time.monotonic() + 3
        raw = bytearray()
        while len(raw) <= 36:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("launcher request deadline")
            sock.settimeout(remaining)
            chunk = sock.recv(37 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) != 36:
            raise ValueError("exact launch identifier required")
        request_id = raw.decode("ascii")
        if str(UUID(request_id)) != request_id:
            raise ValueError("canonical launch identifier required")
        fd = os.open(request_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=state_fd)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(state_fd)
        accepted = launch(request_id) is True
        sock.sendall(b"1" if accepted else b"0")


def request(path, request_id):
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("canonical launch identifier required")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(path)
        _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != 0:
            raise ValueError("root launcher peer required")
        sock.sendall(request_id.encode("ascii"))
        sock.shutdown(socket.SHUT_WR)
        result = sock.recv(2)
        if result not in {b"0", b"1"}:
            raise ValueError("launch response unknown")
        return result == b"1"


def main(argv=None):
    import argparse
    import pwd
    import signal
    import sys

    from .broker_activation import take_listener

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-user", required=True)
    parser.add_argument("--state-directory", required=True)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("Launcher requires a deployment-owned privileged service.", file=sys.stderr)
        return 1
    stopped = []
    previous = {sig: signal.signal(sig, lambda *_: stopped.append(True)) for sig in (signal.SIGINT, signal.SIGTERM)}
    state_fd = None
    try:
        control_uid = pwd.getpwnam(args.control_user).pw_uid
        if control_uid <= 0:
            raise ValueError("independent control account required")
        state_fd = os.open(args.state_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with take_listener() as listener:
            listener.settimeout(0.25)
            while not stopped:
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                if stopped:
                    connection.close()
                    break
                try:
                    serve_connection(connection, control_uid=control_uid, state_fd=state_fd)
                except (ValueError, OSError):
                    pass  # No request contents or recurring notifications.
    except (ValueError, OSError, KeyError):
        print("Launcher unavailable: verify activation and protected paths.", file=sys.stderr)
        return 1
    finally:
        if state_fd is not None:
            os.close(state_fd)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0
