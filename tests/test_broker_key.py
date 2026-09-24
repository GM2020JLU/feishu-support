import os

import pytest

from k3_support.broker_key import load_key_at


@pytest.mark.parametrize("variant", ["valid", "short", "long", "public_file", "public_dir", "symlink", "hardlink", "fifo"])
def test_private_key_objects(tmp_path, variant):
    tmp_path.chmod(0o700)
    key = tmp_path / "broker.key"
    if variant == "fifo":
        os.mkfifo(key, 0o600)
    else:
        key.write_bytes(b"t" * (31 if variant == "short" else 33 if variant == "long" else 32))
        key.chmod(0o600)
    if variant == "public_file":
        key.chmod(0o640)
    elif variant == "public_dir":
        tmp_path.chmod(0o750)
    elif variant == "symlink":
        key.rename(tmp_path / "target")
        key.symlink_to("target")
    elif variant == "hardlink":
        os.link(key, tmp_path / "alias")
    fd = os.open(tmp_path, os.O_DIRECTORY | os.O_RDONLY | os.O_CLOEXEC)
    try:
        if variant == "valid":
            assert load_key_at(fd) == b"t" * 32
        else:
            with pytest.raises(ValueError):
                load_key_at(fd)
        os.fstat(fd)  # loader must not close the caller-owned descriptor
    finally:
        os.close(fd)


@pytest.mark.parametrize("name", ["../secret", "/secret", ".", "..", "", "a\0b"])
def test_key_path_traversal_rejected(name):
    with pytest.raises(ValueError):
        load_key_at(0, name=name)
