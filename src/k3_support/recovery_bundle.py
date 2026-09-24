"""Private offline recovery bundles. Never activate a restored instance.

Callers must quiesce filesystem writers for cross-file consistency. Data-version
and file rechecks detect ordinary concurrent changes but are not a filesystem
snapshot primitive. Bundles retain private configuration and must stay private.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path

from . import __version__
from .db import migration_files
from .operations import OperationsError, restore_probe
from .recovery_inventory import _hash
from .retention_recovery import _parent
from .timeutil import iso_now

ROOTS = ("cases", "attachments", "logs")
MAX_FILES = 10000
MAX_BYTES = 1024 * 1024 * 1024


def _absolute(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise OperationsError("bundle paths must be absolute and non-root")
    return path


def _write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _copy(source, target, remaining):
    expected = _hash(source, remaining)
    parent, name = _parent(source)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OperationsError("source is not a regular file")
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(out, "wb") as output:
            size = 0
            while chunk := os.read(fd, min(1024 * 1024, remaining - size + 1)):
                size += len(chunk)
                if size > remaining:
                    raise OperationsError("bundle exceeds size limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(fd)
    if _hash(target, remaining) != expected or _hash(source, remaining) != expected:
        raise OperationsError("source changed while copying bundle")
    return expected


def _files(root):
    found = []
    for name in ROOTS:
        base = root / name
        if base.is_symlink():
            raise OperationsError("managed artifact root is a symlink")
        if not base.exists():
            continue
        if not base.is_dir():
            raise OperationsError("managed artifact root is not a directory")
        def fail(error):
            raise error

        for directory, dirs, names in os.walk(base, followlinks=False, onerror=fail):
            for child in dirs:
                if (Path(directory) / child).is_symlink():
                    raise OperationsError("artifact directory is a symlink")
            found.extend(Path(directory) / child for child in names)
            if len(found) > MAX_FILES:
                raise OperationsError("bundle exceeds file limit")
    return sorted(found)


def create(config, output):
    target = _absolute(output)
    root = _absolute(config.data_dir)
    database = _absolute(config.database_path)
    config_path = _absolute(config.path)
    if target.is_relative_to(root) or root.is_relative_to(target):
        raise OperationsError("bundle destination must be separate from instance data")
    # Verify all input parents without dereferencing links before SQLite reads.
    for path in (database, config_path):
        parent, name = _parent(path)
        try:
            if not stat.S_ISREG(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                raise OperationsError("bundle input must be a regular file")
        finally:
            os.close(parent)
    parent, name = _parent(target)
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    finally:
        os.close(parent)
    # A failed directory is intentionally retained without manifest.json. It is
    # incomplete evidence, not a valid backup; retries must use a new target.
    _write(target / "INCOMPLETE", b"Not a valid bundle until manifest.json is present.\n")
    files = _files(root)
    source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    entries = []
    try:
        if {row[0] for row in source.execute("SELECT version FROM schema_migrations")} != {
            version for version, _, _ in migration_files()
        }:
            raise OperationsError("bundle requires the exact current schema; migrate separately")
        version = source.execute("PRAGMA data_version").fetchone()[0]
        destination = sqlite3.connect(target / "database.db")
        try:
            deadline = time.monotonic() + 60

            def bounded_backup(_status, _remaining, _total):
                if time.monotonic() > deadline:
                    raise OperationsError("bundle database snapshot timed out")

            source.backup(destination, pages=256, progress=bounded_backup)
            if destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise OperationsError("cannot finalize bundle database")
        finally:
            destination.close()
        (target / "database.db").chmod(0o600)
        restore_probe(target / "database.db")
        db_hash = _hash(target / "database.db", MAX_BYTES)
        entries.append({"path": "database.db", **db_hash})
        snapshot = sqlite3.connect((target / "database.db").as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            refs = snapshot.execute("SELECT raw_artifact_path FROM inbound_events WHERE raw_artifact_path IS NOT NULL")
            included = set(files)
            if any(_absolute(row[0]) not in included for row in refs):
                raise OperationsError("snapshot references an unavailable or unmanaged raw file")
        finally:
            snapshot.close()
        remaining = MAX_BYTES - db_hash["bytes"]
        for original, relative in [(config_path, Path("configuration.yaml"))] + [
            (path, Path("data") / path.relative_to(root)) for path in files
        ]:
            info = _copy(original, target / relative, remaining)
            remaining -= info["bytes"]
            entries.append({"path": str(relative), **info})
        if files != _files(root) or source.execute("PRAGMA data_version").fetchone()[0] != version:
            raise OperationsError("instance changed while creating bundle")
        # Recheck every input, including files copied early in the operation.
        for original, entry in zip([config_path, *files], entries[1:], strict=True):
            if _hash(original, MAX_BYTES) != {key: entry[key] for key in ("bytes", "sha256")}:
                raise OperationsError("instance file changed while creating bundle")
    finally:
        source.close()
    manifest = {
        "format": "k3-support-offline-bundle-v1", "created_at": iso_now(),
        "package_version": __version__, "schema_version": max(v for v, _, _ in migration_files()),
        "original_data_root": str(root), "entries": entries,
        "consistency": "database_snapshot_and_rechecked_files_requires_quiesced_writers",
        "activation_allowed": False, "credential_files_included": False,
        "private_configuration_included": True,
        "excluded": ["credential_files", "external_repositories", "external_release_files", "service_units"],
    }
    _write(target / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    (target / "INCOMPLETE").unlink()
    return {"path": str(target), "files": len(entries), "bytes": MAX_BYTES - remaining,
            "activation_allowed": False, "backup_created": True}


def verify(directory, *, include_manifest=False):
    """Check copied bytes and standalone DB, not authenticity or live readiness."""
    root = _absolute(directory)
    if (root / "INCOMPLETE").exists() or (root / "INCOMPLETE").is_symlink():
        raise OperationsError("bundle is incomplete")
    manifest_path = root / "manifest.json"
    expected = _hash(manifest_path, 8 * 1024 * 1024)
    parent, name = _parent(manifest_path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OperationsError("invalid bundle manifest")
        raw = stream.read(8 * 1024 * 1024 + 1)
    if (len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected["sha256"]
            or _hash(manifest_path, 8 * 1024 * 1024) != expected):
        raise OperationsError("bundle manifest changed")
    try:
        manifest = json.loads(raw)
        if (manifest["format"] != "k3-support-offline-bundle-v1"
                or manifest["activation_allowed"] is not False
                or not isinstance(manifest["entries"], list)
                or not 2 <= len(manifest["entries"]) <= MAX_FILES + 2):
            raise ValueError("invalid manifest contract")
        seen = set()
        total = 0
        for item in manifest["entries"]:
            relative = Path(item["path"])
            if (relative.is_absolute() or ".." in relative.parts or str(relative) != item["path"]
                    or item["path"] in seen
                    or not (str(relative) in {"database.db", "configuration.yaml"}
                            or len(relative.parts) >= 3 and relative.parts[0] == "data" and relative.parts[1] in ROOTS)):
                raise ValueError("invalid bundle member")
            seen.add(item["path"])
            if type(item["bytes"]) is not int or item["bytes"] < 0:
                raise ValueError("invalid member size")
            total += item["bytes"]
            if total > MAX_BYTES:
                raise ValueError("bundle exceeds size limit")
            if _hash(root / relative, MAX_BYTES - total + item["bytes"]) != {
                "bytes": item["bytes"], "sha256": item["sha256"]
            }:
                raise OperationsError("bundle member checksum mismatch")
        if not {"database.db", "configuration.yaml"} <= seen:
            raise ValueError("bundle required member missing")
    except (ValueError, KeyError, TypeError) as exc:
        raise OperationsError("invalid bundle manifest") from exc
    allowed_dirs = {str(parent) for name in seen for parent in Path(name).parents if str(parent) != "."}
    actual = set()

    def fail(error):
        raise error

    for walked_directory, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        for name in dirs:
            path = Path(walked_directory) / name
            if path.is_symlink() or str(path.relative_to(root)) not in allowed_dirs:
                raise OperationsError("unexpected bundle directory")
        for name in names:
            relative = str((Path(walked_directory) / name).relative_to(root))
            if relative not in seen | {"manifest.json"}:
                raise OperationsError("unexpected bundle file")
            actual.add(relative)
    if actual != seen | {"manifest.json"}:
        raise OperationsError("bundle member disappeared")
    restore_probe(root / "database.db")
    result = {"integrity_verified": True, "files": len(seen), "bytes": total,
            "activation_allowed": False, "authenticity_verified": False,
            "path": str(root)}
    if include_manifest:
        result["manifest"] = manifest
        result["manifest_sha256"] = expected["sha256"]
    return result
