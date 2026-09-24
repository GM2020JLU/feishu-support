import os
import socket
import struct

import pytest

from k3_support.broker_owned_listener import owned_listener


def test_listener_peer_is_creator_and_socket_is_removed(tmp_path):
    path = tmp_path / 'broker.sock'
    with owned_listener(path, group_id=os.getegid()) as listener:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
            pid, uid, gid = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            assert (pid, uid, gid) == (os.getpid(), os.geteuid(), os.getegid())
        assert path.stat().st_mode & 0o777 == 0o660
        assert not listener.get_inheritable()
    assert not path.exists()


def test_refuses_active_listener_and_preserves_it(tmp_path):
    path = tmp_path / 'broker.sock'
    with owned_listener(path, group_id=os.getegid()):
        before = path.stat().st_ino
        with pytest.raises(ValueError, match='already active'):
            with owned_listener(path, group_id=os.getegid()):
                pass
        assert path.stat().st_ino == before


def test_recovers_owned_stale_socket(tmp_path):
    path = tmp_path / 'broker.sock'
    with socket.socket(socket.AF_UNIX) as stale:
        stale.bind(str(path))
    with owned_listener(path, group_id=os.getegid()):
        assert path.exists()
    assert not path.exists()


@pytest.mark.parametrize('kind', ['file', 'symlink', 'writable_parent'])
def test_rejects_unsafe_paths(tmp_path, kind):
    path = tmp_path / 'broker.sock'
    if kind == 'file':
        path.write_text('preserve')
    elif kind == 'symlink':
        path.symlink_to(tmp_path / 'missing')
    else:
        tmp_path.chmod(0o777)
    with pytest.raises(ValueError):
        with owned_listener(path, group_id=os.getegid()):
            pass
    if kind == 'file':
        assert path.read_text() == 'preserve'
    if kind == 'symlink':
        assert path.is_symlink()
