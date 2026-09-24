import json
import os
import socket
import struct
from datetime import UTC, datetime

import pytest
from test_broker_receipts import setup

from k3_support.broker_connection import serve_connection
from k3_support.broker_identity import IdentityError, Peer


def exchange(conn, monkeypatch, request, **control_options):
    # Synthetic positive identity; actual distinct-UID acceptance is a separate gate.
    monkeypatch.setattr("k3_support.broker_identity.authenticate_worker",
                        lambda *a, **kw: Peer(pid=42, uid=1234, gid=1234))
    left, right = socket.socketpair()
    with right:
        raw = json.dumps(request).encode()
        right.sendall(struct.pack("!I", len(raw)) + raw)
        serve_connection(conn, left, worker_uid=1234,
                         now=datetime(2026, 9, 8, 0, 30, tzinfo=UTC), **control_options)
        right.settimeout(1)
        chunks = []
        while chunk := right.recv(4096):
            chunks.append(chunk)
        frame = b"".join(chunks)
    assert left.fileno() == -1
    assert struct.unpack("!I", frame[:4])[0] == len(frame) - 4
    return json.loads(frame[4:])


def test_renew_round_trip_and_replay(conn, monkeypatch):
    request = setup(conn)
    first = exchange(conn, monkeypatch, request)
    assert first["result"]["accepted"] is True
    before = list(conn.iterdump())
    assert exchange(conn, monkeypatch, request) == first
    assert list(conn.iterdump()) == before


def test_stale_binding_returns_only_public_error(conn, monkeypatch):
    request = setup(conn)
    request["params"]["lease_token"] = "wrong-secret"
    before = list(conn.iterdump())
    response = exchange(conn, monkeypatch, request)
    assert response["error"] == "stale_binding"
    assert "secret" not in json.dumps(response)
    assert list(conn.iterdump()) == before


def test_unimplemented_method_is_not_acknowledged(conn, monkeypatch):
    request = setup(conn)
    request.update(method="claim", params={"pool": "debug"})
    before = list(conn.iterdump())
    assert exchange(conn, monkeypatch, request)["error"] == "unavailable"
    assert list(conn.iterdump()) == before


def test_actual_wrong_uid_cannot_dispatch_and_socket_closes(conn):
    left, right = socket.socketpair()
    before = list(conn.iterdump())
    with right, pytest.raises(IdentityError):
        serve_connection(conn, left, worker_uid=os.geteuid() + 1)
    assert left.fileno() == -1
    assert list(conn.iterdump()) == before
