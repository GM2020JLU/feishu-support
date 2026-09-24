import json
import os
import socket
import struct
from uuid import uuid4

import pytest

from k3_support.broker_identity import IdentityError, Peer
from k3_support.broker_transport import receive_authenticated_request


def test_real_rejected_peer_leaves_frame_unread():
    left, right = socket.socketpair()
    with left, right:
        right.sendall(b"unread")
        with pytest.raises(IdentityError):
            receive_authenticated_request(left, worker_uid=os.geteuid() + 1)
        assert left.recv(6) == b"unread"


def test_authenticated_transport_preserves_kernel_identity_not_payload(monkeypatch):
    peer = Peer(pid=123, uid=os.geteuid() + 1, gid=456)
    seen = []

    def authenticate(sock, *, worker_uid):
        seen.append(worker_uid)
        return peer

    monkeypatch.setattr("k3_support.broker_identity.authenticate_worker", authenticate)
    left, right = socket.socketpair()
    with left, right:
        value = {"version": 1, "request_id": str(uuid4()), "method": "claim", "params": {"pool": "debug"}}
        raw = json.dumps(value).encode()
        right.sendall(struct.pack("!I", len(raw)) + raw)
        identity, request = receive_authenticated_request(left, worker_uid=peer.uid)
        assert identity is peer and request == value
        assert seen == [peer.uid]
