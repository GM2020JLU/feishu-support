import os
import socket
import threading
from uuid import uuid4

import pytest
from test_review import active_config

from k3_support.broker_identity import Peer
from k3_support.broker_listener import serve
from k3_support.broker_protocol import decode_response, encode_request
from k3_support.broker_transport import receive_frame


def test_real_listener_survives_bad_frame_then_handles_claim(conn, config, tmp_path, monkeypatch):
    monkeypatch.setattr("k3_support.broker_identity.authenticate_worker",
                        lambda *a, **kw: Peer(pid=42, uid=os.geteuid()+1, gid=1234))
    path = str(tmp_path / "broker.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        listener.listen(4)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bad, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as good:
            bad.connect(path)
            bad.sendall(b"\0\0\0\0")
            good.connect(path)
            request = {"version": 1, "request_id": str(uuid4()), "method": "claim", "params": {"pool": "debug"}}
            good.sendall(encode_request(request))
            result = serve(conn, listener, config=active_config(config), worker_uid=os.geteuid()+1,
                           control_key=b"t"*32, stop_event=threading.Event(), max_connections=2)
            assert result == {"connections": 2, "transport_rejections": 1}
            assert decode_response(receive_frame(good), request_id=request["request_id"])["result"] == {"task": None}
        assert listener.gettimeout() is None
        assert listener.fileno() >= 0


def test_preexisting_stop_does_not_accept(conn, config, tmp_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(tmp_path / "broker.sock"))
        listener.listen()
        stop = threading.Event()
        stop.set()
        assert serve(conn, listener, config=config, worker_uid=os.geteuid()+1,
                     control_key=b"t"*32, stop_event=stop)["connections"] == 0


def test_connected_socket_is_not_listener(conn, config):
    left, right = socket.socketpair()
    with left, right, pytest.raises(ValueError, match="listening"):
        serve(conn, left, config=config, worker_uid=os.geteuid()+1,
              control_key=b"t"*32, stop_event=threading.Event())


def test_idle_listener_observes_stop(conn, config, tmp_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(tmp_path / "broker.sock"))
        listener.listen()
        stop = threading.Event()
        timer = threading.Timer(0.02, stop.set)
        timer.start()
        try:
            assert serve(conn, listener, config=config, worker_uid=os.geteuid()+1,
                         control_key=b"t"*32, stop_event=stop)["connections"] == 0
        finally:
            timer.cancel()
            timer.join(timeout=1)
        assert listener.gettimeout() is None
