import os
import socket
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from k3_support.broker_launcher import serve_connection


@pytest.mark.parametrize("payload_kind", ["valid", "duplicate", "extra", "uppercase", "wrong_peer"])
def test_uuid_only_protocol_and_durable_replay_denial(tmp_path, monkeypatch, payload_kind):
    # Real socket/descriptor/file operations; root directory ownership is
    # simulated. This does not claim production privileged-service acceptance.
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original = os.fstat
    monkeypatch.setattr("k3_support.broker_launcher.os.fstat", lambda fd:
                        SimpleNamespace(st_mode=0o40700, st_uid=0) if fd == directory else original(fd))
    request_id = str(uuid4())
    payload = request_id.encode()
    if payload_kind == "extra":
        payload += b" --property=User=root"
    if payload_kind == "uppercase":
        payload = payload.upper()
    if payload_kind == "duplicate":
        (tmp_path / request_id).touch()
    calls = []
    def launch(value):
        assert (tmp_path / value).exists()
        calls.append(value)
        return True
    try:
        left, right = socket.socketpair()
        with right, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(serve_connection, left, control_uid=os.geteuid() + int(payload_kind == "wrong_peer"),
                                 state_fd=directory, launch=launch)
            try:
                right.sendall(payload)
                right.shutdown(socket.SHUT_WR)
            except BrokenPipeError:
                pass
            if payload_kind == "valid":
                assert right.recv(2) == b"1"
                future.result(timeout=5)
                assert calls == [request_id]
            else:
                with pytest.raises((ValueError, OSError)):
                    future.result(timeout=5)
                assert calls == []
    finally:
        os.close(directory)
