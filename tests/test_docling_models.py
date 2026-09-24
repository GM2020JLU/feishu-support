import json

import pytest

from k3_support.docling_models import MANIFEST, inventory, verify
from k3_support.ids import digest


def test_model_identity_detects_changes_and_extra_files(tmp_path):
    model = tmp_path / "weights.bin"
    model.write_bytes(b"synthetic weights")
    manifest = inventory(tmp_path)
    (tmp_path / MANIFEST).write_text(json.dumps(manifest))
    assert verify(tmp_path, digest(manifest))["file_count"] == 1
    model.write_bytes(b"modified weights")
    with pytest.raises(ValueError, match="differ"):
        verify(tmp_path, digest(manifest))
    model.write_bytes(b"synthetic weights")
    (tmp_path / "unexpected").write_bytes(b"x")
    with pytest.raises(ValueError, match="differ"):
        verify(tmp_path, digest(manifest))


def test_model_symlinks_and_empty_bundle_rejected(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        inventory(tmp_path)
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        inventory(tmp_path)


def test_manifest_digest_is_not_self_approved(tmp_path):
    (tmp_path / "weights").write_bytes(b"fixture")
    (tmp_path / MANIFEST).write_text(json.dumps(inventory(tmp_path)))
    with pytest.raises(ValueError, match="digest mismatch"):
        verify(tmp_path, "a" * 64)
