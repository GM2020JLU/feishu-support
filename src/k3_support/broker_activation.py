"""Linux systemd socket activation. Call once, before starting threads."""

import ctypes
import os
import socket


def take_listener():
    """Consume activation metadata and own FD 3 only for the named broker socket.

    libsystemd checks PID/PIDFD identity and sets close-on-exec. Environment
    metadata is an activation convention, not proof of deployment authority.
    """
    name = os.environ.get("LISTEN_FDNAMES")
    try:
        library = ctypes.CDLL("libsystemd.so.0")
        receive = library.sd_listen_fds
        receive.argtypes = [ctypes.c_int]
        receive.restype = ctypes.c_int
        count = receive(1)
    except (OSError, AttributeError) as error:
        raise ValueError("systemd activation unavailable") from error
    # Python's environ mapping is cached separately from libc's unsetenv.
    finally:
        for key in ("LISTEN_PID", "LISTEN_PIDFDID", "LISTEN_FDS", "LISTEN_FDNAMES"):
            os.environ.pop(key, None)
    if count != 1 or name != "broker":
        raise ValueError("one named broker activation socket required")
    listener = None
    try:
        listener = socket.socket(fileno=3)
        if (listener.getsockopt(socket.SOL_SOCKET, socket.SO_DOMAIN) != socket.AF_UNIX
                or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
                or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1):
            raise ValueError("activated Unix listener required")
        listener.set_inheritable(False)
        return listener
    except (OSError, ValueError) as error:
        if listener is not None:
            listener.close()
        raise ValueError("activated listener unavailable") from error
