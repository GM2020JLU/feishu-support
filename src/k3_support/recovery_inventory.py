"""Bounded, read-only coverage audit for raw files referenced by a DB snapshot.

This is not a backup or an authorization to resume the restored workflow.
Hashes describe observed files, not a coordinated filesystem snapshot.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from .operations import OperationsError
from .retention_recovery import _parent


def _hash(path, max_bytes):
    parent_fd, name = _parent(path)
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise OperationsError("unsupported file or file exceeds audit limit")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, min(1024 * 1024, max_bytes - size + 1)):
            size += len(chunk)
            if size > max_bytes:
                raise OperationsError("file grew beyond audit limit")
            digest.update(chunk)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        stamp = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if stamp(before) != stamp(after) or stamp(before) != stamp(current) or size != before.st_size:
            raise OperationsError("file changed during audit")
        return {"bytes": size, "sha256": digest.hexdigest()}
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def audit(conn, config, *, max_files=1000, max_bytes=64 * 1024 * 1024):
    """Audit one bounded set without changing DB rows or following file links."""
    if type(max_files) is not int or not 1 <= max_files <= 10000:
        raise OperationsError("invalid recovery file limit")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024 * 1024:
        raise OperationsError("invalid recovery byte limit")
    rows = conn.execute(
        "SELECT event_pk,raw_artifact_path FROM inbound_events "
        "WHERE raw_artifact_path IS NOT NULL ORDER BY event_pk LIMIT ?",
        (max_files + 1,),
    ).fetchall()
    root = config.data_dir.absolute()
    items = []
    for row in rows[:max_files]:
        path = Path(row["raw_artifact_path"])
        item = {"event_pk": row["event_pk"], "status": "unverified"}
        try:
            if not path.is_absolute() or ".." in path.parts:
                raise OperationsError("noncanonical path")
            relative = path.relative_to(root)
            if len(relative.parts) < 2 or relative.parts[0] not in {"cases", "attachments", "logs"}:
                raise OperationsError("path outside managed artifacts")
            # Relative paths are useful for eventual relocation; no payload is returned.
            item["relative_path"] = str(relative)
            item.update(_hash(path, max_bytes))
            item["status"] = "verified"
        except FileNotFoundError:
            item["status"] = "missing"
        except (OSError, ValueError, OperationsError):
            item["status"] = "unsafe_or_changed"
        items.append(item)
    current = conn.execute(
        "SELECT event_pk,raw_artifact_path FROM inbound_events "
        "WHERE raw_artifact_path IS NOT NULL ORDER BY event_pk LIMIT ?",
        (max_files + 1,),
    ).fetchall()
    references_changed = [tuple(row) for row in current] != [tuple(row) for row in rows]
    return {
        "read_only": True,
        "scope": "referenced_raw_files_only",
        "complete": not references_changed and len(rows) <= max_files and all(item["status"] == "verified" for item in items),
        "references_changed": references_changed,
        "truncated": len(rows) > max_files,
        "items": items,
        "workflow_recoverable": False,
        "excluded": ["unreferenced_files", "configuration", "credentials", "release_artifacts", "approval_fencing"],
    }
