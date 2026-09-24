"""Serialize one instance's board I/O across workers without a long DB transaction."""

import fcntl
import os
import stat
from contextlib import contextmanager


class BoardOperationBusy(ValueError):
    pass


@contextmanager
def hold(config):
    path = config.database_path.resolve().with_suffix(".board-operation.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("unsafe board operation lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BoardOperationBusy(
                "board operation is in progress; retry after it finishes"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
