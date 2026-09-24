"""Instance-local process ownership for irreversible retention operations."""
import fcntl
import os
import stat
from contextlib import contextmanager

from .retention_recovery import _parent


@contextmanager
def hold(config):
    path = config.database_path.absolute().with_suffix('.retention-operation.lock')
    parent, name = _parent(path)
    fd = None
    try:
        fd = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError('unsafe retention process lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('retention operation is still running') from None
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError('retention process lock replaced')
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)
