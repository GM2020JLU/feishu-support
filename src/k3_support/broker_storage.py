"""Fail-closed local metadata checks; not a substitute for distinct-UID canaries."""

import os
import stat
from pathlib import Path


def check_database(path):
    path = Path(path).absolute()
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o077):
        raise ValueError("database directory must be owner-private")
    identity = None
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            if candidate == path:
                raise ValueError("database missing") from None
            continue
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_nlink != 1):
            raise ValueError("database objects must be private regular files")
        if candidate == path:
            identity = (metadata.st_dev, metadata.st_ino)
    return identity
