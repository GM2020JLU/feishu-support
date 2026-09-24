import json
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_broker_receipts import setup

from k3_support.broker_client import exchange
from k3_support.broker_connection import serve_connection
from k3_support.broker_identity import IdentityError, Peer
from k3_support.broker_protocol import ProtocolError, decode_response
from k3_support.broker_transport import receive_request, send_response
from k3_support.db import connect


def request():
    return {"version": 1, "request_id": str(uuid4()), "method": "claim", "params": {"pool": "debug"}}


def test_client_real_socket_roundtrip(monkeypatch):
    value = request()
    monkeypatch.setattr("k3_support.broker_client.authenticate_worker",
                        lambda *a, **kw: Peer(pid=42, uid=1234, gid=1234))
    client, server = socket.socketpair()

    def respond():
        with server:
            seen = receive_request(server)
            assert seen == value
            send_response(server, request_id=seen["request_id"], error_code="unavailable")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(respond)
        response = exchange(client, value, control_uid=1234)
        future.result(timeout=2)
    assert response["error"] == "unavailable"
    assert client.fileno() == -1


def test_wrong_server_uid_receives_no_secret():
    client, server = socket.socketpair()
    with server:
        with pytest.raises(IdentityError):
            exchange(client, request(), control_uid=os.geteuid() + 1)
        assert server.recv(1) == b""
    assert client.fileno() == -1


@pytest.mark.parametrize("mutation", [
    {"version": True}, {"version": 2}, {"ok": 1}, {"request_id": str(uuid4())},
    {"extra": "ignored?"}, {"result": []}, {"error": "unavailable"},
])
def test_response_schema_and_correlation(mutation):
    rid = str(uuid4())
    value = {"version": 1, "request_id": rid, "ok": True, "result": {}}
    value.update(mutation)
    with pytest.raises(ProtocolError):
        decode_response(json.dumps(value).encode(), request_id=rid)


def test_duplicate_response_field_rejected():
    rid = str(uuid4())
    raw = ('{"version":1,"request_id":"' + rid + '","ok":true,"result":{},"result":{}}').encode()
    with pytest.raises(ProtocolError):
        decode_response(raw, request_id=rid)


def test_stalled_server_times_out_without_retry(monkeypatch):
    monkeypatch.setattr("k3_support.broker_client.authenticate_worker",
                        lambda *a, **kw: Peer(pid=42, uid=1234, gid=1234))
    client, server = socket.socketpair()
    value = request()
    with server:
        with pytest.raises(ProtocolError):
            exchange(client, value, control_uid=1234, timeout=0.01)
        assert receive_request(server) == value
        assert server.recv(1) == b""
    assert client.fileno() == -1


def test_client_to_renew_transaction_and_replay(conn, config, monkeypatch):
    value = setup(conn)
    identity = lambda *a, **kw: Peer(pid=42, uid=1234, gid=1234)
    monkeypatch.setattr("k3_support.broker_client.authenticate_worker", identity)
    monkeypatch.setattr("k3_support.broker_identity.authenticate_worker", identity)

    def roundtrip():
        client, server = socket.socketpair()

        def serve():
            db = connect(config.database_path)
            try:
                serve_connection(db, server, worker_uid=1234,
                                 now=datetime(2026, 9, 8, 0, 30, tzinfo=UTC))
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(serve)
            response = exchange(client, value, control_uid=1234)
            future.result(timeout=2)
        return response

    first = roundtrip()
    assert first["result"] == {"accepted": True, "job_id": "job-1",
                               "expires_at": "2026-09-08T00:35:00+00:00"}
    before = list(conn.iterdump())
    assert roundtrip() == first
    assert list(conn.iterdump()) == before
    assert conn.execute("SELECT count(*) FROM broker_receipts").fetchone()[0] == 1
