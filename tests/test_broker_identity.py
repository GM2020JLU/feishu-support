import os
import socket
import struct

import pytest

from k3_support.broker_identity import IdentityError, authenticate_worker


def test_real_socket_rejects_same_identity_and_wrong_kernel_uid():
    left, right = socket.socketpair()
    with left, right:
        with pytest.raises(IdentityError, match="must differ"):
            authenticate_worker(left, worker_uid=os.geteuid())
        with pytest.raises(IdentityError, match="not authorized"):
            authenticate_worker(left, worker_uid=os.geteuid() + 1)


def test_synthetic_credentials_only_accept_configured_distinct_uid():
    class Fixture:
        family = socket.AF_UNIX

        def getpeername(self):
            return "synthetic"

        def getsockopt(self, level, option, *args):
            if option == socket.SO_TYPE:
                return socket.SOCK_STREAM
            assert option == socket.SO_PEERCRED
            return struct.pack("iII", 123, os.geteuid() + 1, 456)

    result = authenticate_worker(Fixture(), worker_uid=os.geteuid() + 1)
    assert result.uid == os.geteuid() + 1 and result.pid == 123


@pytest.mark.parametrize("uid", [True, "1000", -1, 0, 4294967295, None])
def test_invalid_uid_fails_before_socket_access(uid):
    with pytest.raises(IdentityError):
        authenticate_worker(None, worker_uid=uid)
