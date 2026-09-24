import subprocess
import sys

import pytest

from k3_support.retention_operation_guard import hold


def test_real_child_lock_blocks_until_process_exit(config, conn):
    path = config.database_path.absolute().with_suffix('.retention-operation.lock')
    code = "import fcntl,os,sys; f=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()"
    child = subprocess.Popen([sys.executable, '-I', '-c', code, str(path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        import select
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline().strip() == 'locked'
        assert child.poll() is None
        with pytest.raises(ValueError, match='still running'):
            with hold(config):
                pytest.fail('lock acquired while child holds it')
        child.communicate('', timeout=5)
        assert child.returncode == 0
        with hold(config):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


def test_guard_rejects_symlink_and_public_file(config, conn, tmp_path):
    path = config.database_path.absolute().with_suffix('.retention-operation.lock')
    target = tmp_path / 'unrelated'
    target.write_text('keep')
    path.symlink_to(target)
    with pytest.raises(OSError):
        with hold(config):
            pass
    path.unlink()
    path.write_text('')
    path.chmod(0o644)
    with pytest.raises(ValueError, match='unsafe'):
        with hold(config):
            pass
    assert target.read_text() == 'keep'
