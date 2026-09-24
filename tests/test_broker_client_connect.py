import os
import socket
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_broker_client import request

from k3_support.broker_client import request_at
from k3_support.broker_identity import IdentityError, Peer
from k3_support.broker_protocol import ProtocolError
from k3_support.broker_transport import receive_request, send_response


def test_connect_and_exchange_real_filesystem_socket(tmp_path, monkeypatch):
    monkeypatch.setattr("k3_support.broker_client.authenticate_worker",
                        lambda *a, **kw: Peer(pid=42, uid=1234, gid=1234))
    path = str(tmp_path / "broker.sock")
    value = request()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        listener.listen()
        listener.settimeout(2)
        def serve():
            connection, _ = listener.accept()
            with connection:
                assert receive_request(connection) == value
                send_response(connection, request_id=value["request_id"], result={"task": None})
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(serve)
            assert request_at(path, value, control_uid=1234)["result"] == {"task": None}
            future.result(timeout=2)


def test_wrong_real_peer_gets_no_request_bytes(tmp_path):
    path = str(tmp_path / "broker.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        listener.listen()
        listener.settimeout(1)
        with pytest.raises(IdentityError):
            request_at(path, request(), control_uid=os.geteuid()+1)
        peer, _ = listener.accept()
        with peer:
            peer.settimeout(1)
            assert peer.recv(1) == b""


@pytest.mark.parametrize("path", ["relative.sock", "\0abstract", "/" + "a" * 108, None])
def test_invalid_socket_paths(path):
    with pytest.raises(ProtocolError):
        request_at(path, request(), control_uid=1234)


def test_missing_socket_has_bounded_public_error(tmp_path):
    with pytest.raises(ProtocolError, match="connection unavailable"):
        request_at(str(tmp_path / "missing.sock"), request(), control_uid=1234)
