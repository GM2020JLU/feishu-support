import os

import pytest

from k3_support.broker_storage import check_database


@pytest.mark.parametrize("variant", ["valid", "public_directory", "public_database", "symlink",
                                    "hardlink", "public_wal", "linked_shm"])
def test_private_database_metadata(tmp_path, variant):
    private = tmp_path / "db"
    private.mkdir(mode=0o700)
    db = private / "support.db"
    db.write_bytes(b"synthetic metadata fixture")
    db.chmod(0o600)
    if variant == "public_directory":
        private.chmod(0o750)
    elif variant == "public_database":
        db.chmod(0o640)
    elif variant == "symlink":
        db.rename(private / "target")
        db.symlink_to("target")
    elif variant == "hardlink":
        os.link(db, private / "alias")
    elif variant == "public_wal":
        wal = private / "support.db-wal"
        wal.write_bytes(b"synthetic")
        wal.chmod(0o644)
    elif variant == "linked_shm":
        (private / "support.db-shm").symlink_to("support.db")
    if variant == "valid":
        assert check_database(db) == (db.stat().st_dev, db.stat().st_ino)
    else:
        with pytest.raises(ValueError):
            check_database(db)
