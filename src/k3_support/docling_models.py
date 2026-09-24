"""Content identity for explicitly provisioned, read-only Docling model bundles.

Call inside the supervised converter so hashing shares its execution deadline.
Model directories are trusted deployment inputs, not attachment-supplied paths.
"""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .ids import digest

MANIFEST = "model-manifest.json"


def inventory(root: Path) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("model root must be a real directory")
    files = {}
    total = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        if any((Path(directory) / name).is_symlink() for name in dirs):
            raise ValueError("model directory symlinks are forbidden")
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == MANIFEST:
                continue
            if len(files) >= 10000:
                raise ValueError("too many model files")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("models must be regular files")
                total += before.st_size
                if total > 16 * 1024**3:
                    raise ValueError("model bundle exceeds 16 GiB")
                sha = hashlib.sha256()
                remaining = before.st_size
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("model file changed while hashing")
                    sha.update(chunk)
                    remaining -= len(chunk)
                after = os.fstat(stream.fileno())
                if stream.read(1) or (before.st_size, before.st_mtime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValueError("model file changed while hashing")
            files[relative] = {"sha256": sha.hexdigest(), "bytes": before.st_size}
    if not files:
        raise ValueError("model bundle is empty")
    return {"schema": "k3-docling-models-v1", "files": files}


def verify(root: Path, expected_digest: str) -> dict:
    if not isinstance(expected_digest, str) or not re.fullmatch(
        "[0-9a-f]{64}", expected_digest
    ):
        raise ValueError("explicit model manifest digest required")
    fd = os.open(root / MANIFEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("model manifest must be a regular file")
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("model manifest exceeds limit")
    manifest = json.loads(raw)
    if digest(manifest) != expected_digest:
        raise ValueError("model manifest digest mismatch")
    if inventory(root) != manifest:
        raise ValueError("model files differ from the approved manifest")
    return {"manifest_digest": expected_digest, "file_count": len(manifest["files"])}
