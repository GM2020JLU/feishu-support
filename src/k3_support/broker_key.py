"""Read a control key relative to an already opened private directory descriptor."""

import os
import stat


def load_key_at(directory_fd, *, name="broker.key"):
    """Caller owns directory_fd; no path traversal, creation, repair or key logging.

    This protects the opened objects, not a deployment's process/FD inheritance.
    The worker must never inherit this descriptor or the returned key.
    """
    if type(directory_fd) is not int or directory_fd < 0:
        raise ValueError("private directory descriptor required")
    if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise ValueError("single key filename required")
    fd = None
    try:
        parent = os.fstat(directory_fd)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) & 0o077):
            raise ValueError("control directory must be owner-private")
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) & 0o077 or before.st_nlink != 1
                or before.st_size != 32):
            raise ValueError("invalid control key metadata")
        key = os.read(fd, 33)
        after = os.fstat(fd)
        if len(key) != 32 or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("control key changed while reading")
        return key
    except OSError as error:
        raise ValueError("control key unavailable") from error
    finally:
        if fd is not None:
            os.close(fd)
