"""Stage a verified bundle into a NEW private directory, never an active DB."""
import json
import os
import sqlite3

import yaml

from .config import FEATURES, Config, validate_config
from .operations import OperationsError, restore_probe
from .recovery_bundle import MAX_BYTES, _absolute, _copy, _write, verify
from .recovery_fence import fence
from .retention_recovery import _parent


def stage(bundle, output):
    source, target = _absolute(bundle), _absolute(output)
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise OperationsError("restore destination must be separate from bundle")
    verified = verify(source, include_manifest=True)
    manifest = verified["manifest"]
    parent, name = _parent(target)
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    finally:
        os.close(parent)
    _write(target / "INCOMPLETE", b"Offline restore not completed. Do not start services.\n")
    for item in manifest["entries"]:
        info = _copy(source / item["path"], target / item["path"], MAX_BYTES)
        if info != {key: item[key] for key in ("bytes", "sha256")}:
            raise OperationsError("bundle changed while staging")
    # The copied configuration remains an archive, never overwritten in place.
    # Expand defaults first: absent notification/routing blocks must not regain
    # enabled defaults after we have disabled the archived explicit settings.
    data = validate_config(yaml.safe_load((target / "configuration.yaml").read_text()))
    data["mode"] = "shadow"
    data["paths"] = {"data_dir": str(target / "data"), "database": str(target / "database.db")}
    data["features"] = dict.fromkeys(FEATURES, False)
    if "notifications" in data:
        data["notifications"] = dict.fromkeys(data["notifications"], False)
    if "routing" in data:
        data["routing"]["ai_enabled"] = False
    data = validate_config(data)
    config = Config(data, target / "config.review.yaml")
    conn = sqlite3.connect(target / "database.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        status = fence(conn)
        old_root = _absolute(manifest["original_data_root"])

        def relocate(value):
            old = _absolute(value)
            try:
                relative = old.relative_to(old_root)
            except ValueError as exc:
                raise OperationsError("unmanaged recovery artifact path") from exc
            if not relative.parts or relative.parts[0] not in {"cases", "attachments", "logs"}:
                raise OperationsError("unmanaged recovery artifact path")
            return str(config.data_dir / relative)

        conn.execute("BEGIN IMMEDIATE")
        try:
            for row in conn.execute("SELECT event_pk,raw_artifact_path FROM inbound_events WHERE raw_artifact_path IS NOT NULL").fetchall():
                conn.execute("UPDATE inbound_events SET raw_artifact_path=? WHERE event_pk=?",
                             (relocate(row["raw_artifact_path"]), row["event_pk"]))
            for row in conn.execute("SELECT attempt_id,original_path,quarantine_path FROM retention_attempts").fetchall():
                conn.execute("UPDATE retention_attempts SET original_path=?,quarantine_path=? WHERE attempt_id=?",
                             (relocate(row["original_path"]), relocate(row["quarantine_path"]), row["attempt_id"]))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if list(conn.execute("PRAGMA foreign_key_check")):
            raise OperationsError("restored references failed integrity check")
    finally:
        conn.close()
    restore_probe(target / "database.db")
    _write(config.path, yaml.safe_dump(data, allow_unicode=True).encode())
    report = {"staged": True, "activation_allowed": False, "path": str(target),
              "source_manifest_sha256": verified["manifest_sha256"], "fencing": status,
              "unrelocated": ["historical_job_workdirs", "external_runtime_paths", "external_evidence_paths"],
              "credentials_restored": False}
    _write(target / "restore-report.json", json.dumps(report, indent=2).encode())
    (target / "INCOMPLETE").unlink()
    return report
