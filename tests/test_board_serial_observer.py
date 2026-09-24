import json
import os
import socket
import threading

import pytest

from k3_support.board_serial_observer import observe_reset


@pytest.mark.parametrize("mode", ["immediate", "bad_ack", "wrong_uid", "stale", "disconnect", "revoked"])
def test_real_socket_requires_ack_before_reset_and_captures_instant_marker(tmp_path, mode):
    path = str(tmp_path / "serial.sock")
    reset = threading.Event()
    ready = threading.Event()
    errors = []
    triggered = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        server.listen(1)
        server.settimeout(3)
        def daemon():
            try:
                with server.accept()[0] as client:
                    client.settimeout(3)
                    raw = bytearray()
                    while not raw.endswith(b"\n"):
                        data = client.recv(4096)
                        if not data:
                            return
                        raw.extend(data)
                    assert json.loads(raw)["mode"] == "read"
                    ack = {"ok": True, "version": 99 if mode == "bad_ack" else 2, "rx_seq": 12}
                    ready.set()
                    tail = b"ROM: usb download handler" if mode == "stale" else b""
                    client.sendall(json.dumps(ack).encode()+b"\n"+tail)
                    if mode == "bad_ack":
                        return
                    if reset.wait(2) and mode == "immediate":
                        client.sendall(b"ROM: usb download handler")
            except Exception as exc:  # noqa: BLE001 - relay worker-thread failures to assertions
                errors.append(exc)
        thread = threading.Thread(target=daemon)
        thread.start()
        def trigger():
            assert ready.is_set()
            triggered.append(True)
            reset.set()
        def heartbeat():
            if mode == "revoked" and ready.is_set():
                raise ValueError("revoked")
        try:
            kwargs = {"socket_path": path, "daemon_uid": os.getuid()+(mode == "wrong_uid"),
                          "trigger": trigger, "heartbeat": heartbeat, "timeout": 1}
            if mode == "immediate":
                assert observe_reset(**kwargs)["output"] == "ROM: usb download handler"
            else:
                with pytest.raises((ValueError, TimeoutError)):
                    observe_reset(**kwargs)
            assert bool(triggered) == (mode in ("immediate", "stale", "disconnect"))
        finally:
            reset.set()
            thread.join(4)
        assert not thread.is_alive()
        assert not errors
