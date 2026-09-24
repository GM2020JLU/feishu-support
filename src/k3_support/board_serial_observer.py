"""Control-side serial v2 observer: attach acknowledgement precedes reset.

The socket path and daemon UID are trusted deployment inputs, never worker tool
arguments. This adapter does not start daemons, acquire writers or read logs.
"""

import json
import os
import select
import socket
import struct
import time

from .board_serial_evidence import fresh_match


def endpoint(runtime):
    path, uid = runtime.get("board_serial_socket"), runtime.get("board_serial_daemon_uid")
    if path is None and uid is None:
        return None
    if (not isinstance(path, str) or not path.startswith("/") or "\0" in path
            or len(path.encode()) > 107 or any(p in ("", ".", "..") for p in path.split("/")[1:])
            or type(uid) is not int or uid < 0):
        raise ValueError("board serial socket and daemon UID must be configured together with a canonical socket path")
    return path, uid


def observe_reset(*, socket_path, daemon_uid, trigger, heartbeat, timeout=45):
    if type(daemon_uid) is not int or daemon_uid < 0 or type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError("invalid observer deployment parameters")
    if not isinstance(socket_path, str) or not socket_path.startswith("/") or "\0" in socket_path:
        raise ValueError("absolute serial socket required")
    marker = "usb_init : enter|usb_core_init : enter|ROM: usb download handler"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        heartbeat()
        client.connect(socket_path)
        _, uid, _ = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != daemon_uid:
            raise ValueError("serial daemon identity mismatch")
        client.sendall(json.dumps({"op": "attach", "version": 2, "mode": "read",
                                   "name": "k3-support:brom-observer", "pid": os.getpid()}).encode()+b"\n")
        data = bytearray()
        deadline = time.monotonic()+3
        while b"\n" not in data:
            heartbeat()
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError("serial observer readiness timeout")
            client.settimeout(min(.2, remaining))
            try:
                chunk = client.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                raise ValueError("serial disconnected before readiness")
            data.extend(chunk)
            if len(data) > 8192:
                raise ValueError("oversized serial acknowledgement")
        head, _, _ = data.partition(b"\n")
        hello = json.loads(head)
        if (not isinstance(hello, dict) or hello.get("ok") is not True
                or type(hello.get("version")) is not int or hello["version"] != 2
                or type(hello.get("rx_seq")) is not int or hello["rx_seq"] < 0):
            raise ValueError("invalid serial readiness acknowledgement")
        # Exclude bytes received before reset, including any ACK-frame tail.
        drained = 0
        while select.select([client], [], [], 0)[0]:
            chunk = client.recv(4096)
            if not chunk:
                raise ValueError("serial disconnected before reset")
            drained += len(chunk)
            if drained > 128000:
                raise ValueError("serial stream did not quiesce before reset")
        heartbeat()
        trigger()  # Must raise on unsuccessful reset; no lease is released here.
        deadline = time.monotonic()+timeout
        output = bytearray()
        while time.monotonic() < deadline:
            heartbeat()
            if not select.select([client], [], [], min(.2, max(0, deadline-time.monotonic())))[0]:
                continue
            chunk = client.recv(4096)
            if not chunk:
                raise ValueError("serial disconnected during reset observation")
            output.extend(chunk)
            if len(output) > 128000:
                raise ValueError("serial observation output limit exceeded")
            receipt = {"ok": True, "fresh": True, "matched": True,
                       "rx_seq_start": hello["rx_seq"], "output": output.decode("utf-8", "replace")}
            try:
                fresh_match(json.dumps(receipt), marker)
            except ValueError:
                continue
            heartbeat()
            return receipt
        raise TimeoutError("fresh ROM marker not observed")
