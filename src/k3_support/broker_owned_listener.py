"""Create a listener as the control UID so SO_PEERCRED identifies the server."""

import errno
import os
import socket
import stat
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def owned_listener(path, *, group_id):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or type(group_id) is not int or group_id < 0:
        raise ValueError('absolute listener path and group required')
    # A private owner-writable parent prevents replacement during stale cleanup.
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or parent.st_mode & 0o022 or path.parent.resolve() != path.parent):
        raise ValueError('protected control-owned listener directory required')
    try:
        old = path.lstat()
    except FileNotFoundError:
        old = None
    if old is not None:
        if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.geteuid():
            raise ValueError('refusing to replace foreign listener path')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                probe.connect(str(path))
            except OSError as error:
                if error.errno != errno.ECONNREFUSED:
                    raise ValueError('listener availability unknown') from error
            else:
                raise ValueError('listener already active')
        if path.lstat() != old:
            raise ValueError('listener changed during inspection')
        path.unlink()
    identity = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(str(path))
            info = path.lstat()
            identity = (info.st_dev, info.st_ino)
            os.chown(path, -1, group_id)
            path.chmod(0o660)
            listener.listen(16)
            listener.set_inheritable(False)
            yield listener
        finally:
            if identity is not None:
                try:
                    info = path.lstat()
                    if (info.st_dev, info.st_ino) == identity:
                        path.unlink()
                except FileNotFoundError:
                    pass
