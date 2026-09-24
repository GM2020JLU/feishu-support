"""Linux connected Unix peer authentication; no client-supplied identity is trusted."""

import os
import socket
import struct
from dataclasses import dataclass


class IdentityError(ValueError):
    pass


@dataclass(frozen=True)
class Peer:
    pid: int
    uid: int
    gid: int


def authenticate_worker(sock, *, worker_uid):
    """Authenticate connection-time credentials, not current PID ownership or task rights."""
    if type(worker_uid) is not int or not 0 < worker_uid < 4294967295:
        raise IdentityError("invalid worker UID")
    if worker_uid == os.geteuid():
        raise IdentityError("worker and control identities must differ")
    if sock.family != socket.AF_UNIX or sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
        raise IdentityError("connected Unix stream required")
    if not hasattr(socket, "SO_PEERCRED"):
        raise IdentityError("kernel peer credentials unavailable")
    try:
        sock.getpeername()
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII"))
        pid, uid, gid = struct.unpack("iII", raw)
    except (OSError, struct.error) as error:
        raise IdentityError("kernel peer credentials unavailable") from error
    if pid <= 0 or uid != worker_uid:
        raise IdentityError("peer not authorized")
    return Peer(pid=pid, uid=uid, gid=gid)
