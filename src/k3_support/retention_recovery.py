"""Recoverable raw-file retirement. No automatic irreversible purge.

The durable intent precedes filesystem mutation. A crash may leave a prepared
intent, never an unrecorded unlink. Both path traversal and recovery fail closed.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .db import transaction
from .ids import canonical_json, digest, new_id
from .timeutil import iso_now


def _parent(path):
    path = Path(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = following
        return fd, path.name
    except BaseException:
        os.close(fd)
        raise


def _signature(value):
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def prepare(conn, config, item):
    from .operations import _safe_artifact

    path = _safe_artifact(config, item["path"])
    attempt_id = new_id("ret")
    quarantine = path.parent / (".retention-" + attempt_id) / "payload"
    with transaction(conn):
        existing = conn.execute(
            "SELECT * FROM retention_attempts WHERE event_pk=? AND state IN ('prepared','quarantined')",
            (item["event_pk"],),
        ).fetchone()
        if existing:
            return dict(existing)
        conn.execute(
            """INSERT INTO retention_attempts(attempt_id,event_pk,original_path,
                     quarantine_path,file_stamp_json,received_at,state,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,'prepared',?,?)""",
            (
                attempt_id,
                item["event_pk"],
                str(path),
                str(quarantine),
                canonical_json(item["file_stamp"]),
                item["received_at"],
                iso_now(),
                iso_now(),
            ),
        )
    return dict(
        conn.execute(
            "SELECT * FROM retention_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
    )


def quarantine(conn, config, attempt):
    """Caller owns the DB write transaction and has just rechecked references."""
    from .operations import OperationsError, _safe_artifact

    path = _safe_artifact(config, attempt["original_path"])
    target = _safe_artifact(config, attempt["quarantine_path"])
    source_fd, source_name = _parent(path)
    target_fd = None
    try:
        expected = json.loads(attempt["file_stamp_json"])
        if (
            _signature(os.stat(source_name, dir_fd=source_fd, follow_symlinks=False))
            != expected[:4]
        ):
            raise OperationsError("retention source changed before quarantine")
        os.mkdir(target.parent.name, mode=0o700, dir_fd=source_fd)
        target_fd = os.open(
            target.parent.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=source_fd,
        )
        os.rename(source_name, "payload", src_dir_fd=source_fd, dst_dir_fd=target_fd)
        os.fsync(source_fd)
        os.fsync(target_fd)
        moved = os.stat("payload", dir_fd=target_fd, follow_symlinks=False)
        if _signature(moved) != expected[:4]:
            raise OperationsError(
                "retention source replaced during quarantine; recovery required"
            )
        conn.execute(
            "UPDATE inbound_events SET raw_artifact_path=? WHERE event_pk=? AND raw_artifact_path=?",
            (str(target), attempt["event_pk"], str(path)),
        )
        conn.execute(
            "UPDATE retention_attempts SET state='quarantined',updated_at=? WHERE attempt_id=? AND state='prepared'",
            (iso_now(), attempt["attempt_id"]),
        )
        return moved.st_size
    finally:
        if target_fd is not None:
            os.close(target_fd)
        os.close(source_fd)


def _recovery_digest(conn, row):
    current = conn.execute("SELECT raw_artifact_path FROM inbound_events WHERE event_pk=?", (row["event_pk"],)).fetchone()
    return digest({"attempt": dict(row), "current_path": current[0] if current else None})


def recovery_preview(conn, attempt_id):
    if not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 100:
        raise ValueError("invalid retention attempt")
    with transaction(conn):
        row = conn.execute("SELECT * FROM retention_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None or row["state"] not in ("prepared", "quarantined", "failed"):
            raise ValueError("此记录不需要恢复，请重新读取状态")
        return {"attempt_id": attempt_id, "event_pk": row["event_pk"], "state": row["state"],
                "binding_digest": _recovery_digest(conn, row),
                "summary": "将恢复此记录对应的隔离原件，不覆盖原位置已有的其他文件；执行时重新核对文件。不会重新启用 AI 或发送消息。"}


def apply_recovery(conn, config, *, attempt_id, binding_digest, request_id, actor_id):
    from uuid import UUID

    if (not isinstance(request_id, str) or str(UUID(request_id)) != request_id
            or not isinstance(actor_id, str) or not actor_id or len(actor_id) > 256):
        raise ValueError("invalid recovery request identity")
    with transaction(conn):
        prior = conn.execute("SELECT * FROM retention_recovery_requests WHERE request_id=?", (request_id,)).fetchone()
        if prior:
            if (prior["attempt_id"], prior["actor_id"], prior["binding_digest"]) != (attempt_id, actor_id, binding_digest):
                raise ValueError("recovery request binding changed")
            return {"attempt_id": attempt_id, "state": "restored" if prior["state"] == "restored" else "unknown", "replayed": True}
        row = conn.execute("SELECT * FROM retention_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None or row["state"] not in ("prepared", "quarantined", "failed"):
            raise ValueError("此记录不需要恢复，请重新读取状态")
        if _recovery_digest(conn, row) != binding_digest:
            raise ValueError("恢复预览已失效，请重新读取并确认")
        if conn.execute("SELECT 1 FROM retention_recovery_requests WHERE attempt_id=? AND state IN ('running','unknown')", (attempt_id,)).fetchone():
            raise ValueError("已有恢复请求待核对，不自动重复操作")
        conn.execute("INSERT INTO retention_recovery_requests VALUES(?,?,?,?,'running',?,?)",
                     (request_id, attempt_id, actor_id, binding_digest, iso_now(), iso_now()))
    try:
        result = recover(conn, config, attempt_id, expected_digest=binding_digest)
    except Exception:
        with transaction(conn):
            conn.execute("UPDATE retention_recovery_requests SET state='unknown',updated_at=? WHERE request_id=?", (iso_now(), request_id))
        raise
    with transaction(conn):
        conn.execute("UPDATE retention_recovery_requests SET state='restored',updated_at=? WHERE request_id=?", (iso_now(), request_id))
    return result


def check_recovery(conn, config, *, request_id, actor_id):
    """Reconcile completed restoration evidence; never move or read file bytes."""
    from datetime import datetime

    from .operations import OperationsError, _safe_artifact

    with transaction(conn):
        request = conn.execute("SELECT * FROM retention_recovery_requests WHERE request_id=?", (request_id,)).fetchone()
        if request is None or request["actor_id"] != actor_id:
            raise ValueError("recovery request unavailable")
        if request["state"] == "restored":
            return {"state": "restored", "historical": True}
        row = conn.execute("SELECT * FROM retention_attempts WHERE attempt_id=?", (request["attempt_id"],)).fetchone()
        if row is None or row["state"] != "restored":
            return {"state": "unknown"}
        try:
            created, updated = (datetime.fromisoformat(v) for v in (request["created_at"], row["updated_at"]))
            if created.tzinfo is None or updated.tzinfo is None or updated < created:
                return {"state": "unknown"}
            source = _safe_artifact(config, row["original_path"])
            target = _safe_artifact(config, row["quarantine_path"])
            current = conn.execute("SELECT raw_artifact_path FROM inbound_events WHERE event_pk=?", (row["event_pk"],)).fetchone()
            if current is None or current[0] != str(source):
                return {"state": "unknown"}
            fd, name = _parent(source)
            try:
                value = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(value.st_mode) or _signature(value) != json.loads(row["file_stamp_json"])[:4]:
                    return {"state": "unknown"}
            finally:
                os.close(fd)
            fd, name = _parent(target)
            try:
                try:
                    os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    return {"state": "unknown"}
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError, OperationsError):
            return {"state": "unknown"}
        conn.execute("UPDATE retention_recovery_requests SET state='restored',updated_at=? WHERE request_id=?", (iso_now(), request_id))
        return {"state": "restored", "reconciled": True}


def recover(conn, config, attempt_id, *, expected_digest=None):
    from .operations import OperationsError, _safe_artifact

    with transaction(conn):
        if conn.execute("SELECT 1 FROM retention_purge_requests WHERE attempt_id=? AND state IN ('running','unknown','purged')", (attempt_id,)).fetchone():
            raise OperationsError('permanent purge pending or complete; recovery cannot race it')
        row = conn.execute(
            "SELECT * FROM retention_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise OperationsError("retention attempt not found")
        if expected_digest is not None and (not isinstance(expected_digest, str) or _recovery_digest(conn, row) != expected_digest):
            raise OperationsError("恢复预览已失效，请重新读取并确认")
        if row["state"] in {"restored", "cancelled"}:
            return {"attempt_id": attempt_id, "state": row["state"], "replayed": True}
        source = _safe_artifact(config, row["original_path"])
        target = _safe_artifact(config, row["quarantine_path"])
        current = conn.execute(
            "SELECT raw_artifact_path FROM inbound_events WHERE event_pk=?",
            (row["event_pk"],),
        ).fetchone()
        if current is None or current[0] not in {str(source), str(target)}:
            raise OperationsError(
                "retention source binding changed; both files retained"
            )
        source_fd, source_name = _parent(source)
        target_fd = None
        try:
            if target.exists():
                target_fd, target_name = _parent(target)
                saved = os.stat(target_name, dir_fd=target_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(saved.st_mode)
                    or _signature(saved) != json.loads(row["file_stamp_json"])[:4]
                ):
                    raise OperationsError(
                        "quarantined file changed; manual recovery required"
                    )
                # link is no-clobber: a new file at the original name survives.
                try:
                    os.link(
                        target_name,
                        source_name,
                        src_dir_fd=target_fd,
                        dst_dir_fd=source_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    original = os.stat(
                        source_name, dir_fd=source_fd, follow_symlinks=False
                    )
                    saved = os.stat(
                        target_name, dir_fd=target_fd, follow_symlinks=False
                    )
                    if (original.st_dev, original.st_ino) != (
                        saved.st_dev,
                        saved.st_ino,
                    ):
                        raise OperationsError(
                            "original path occupied; quarantined file retained"
                        ) from None
                os.fsync(source_fd)
                os.unlink(target_name, dir_fd=target_fd)
                os.fsync(target_fd)
            else:
                value = os.stat(source_name, dir_fd=source_fd, follow_symlinks=False)
                if _signature(value) != json.loads(row["file_stamp_json"])[:4]:
                    raise OperationsError(
                        "retention original changed; manual recovery required"
                    )
            conn.execute(
                "UPDATE inbound_events SET raw_artifact_path=? WHERE event_pk=?",
                (str(source), row["event_pk"]),
            )
            conn.execute(
                "UPDATE retention_attempts SET state='restored',updated_at=? WHERE attempt_id=?",
                (iso_now(), attempt_id),
            )
        finally:
            if target_fd is not None:
                os.close(target_fd)
            os.close(source_fd)
        return {"attempt_id": attempt_id, "state": "restored", "replayed": False}


def reconcile_retention(conn, config):
    from .operations import OperationsError, _retention_referenced

    results = []
    with transaction(conn):
        cursor = conn.execute(
            "SELECT last_attempt_id FROM retention_reconcile_cursor WHERE singleton=1"
        ).fetchone()[0]
        query = """SELECT * FROM retention_attempts WHERE state IN ('prepared','quarantined')
                   AND attempt_id>? ORDER BY attempt_id LIMIT 100"""
        rows = conn.execute(query, (cursor,)).fetchall()
        if not rows and cursor:
            rows = conn.execute(query, ("",)).fetchall()
        conn.execute(
            "UPDATE retention_reconcile_cursor SET last_attempt_id=? WHERE singleton=1",
            (rows[-1]["attempt_id"] if rows else "",),
        )
    # Advance held/error entries as well, so they cannot starve later work.
    for row in rows:
        if row["state"] == "prepared" or _retention_referenced(
            conn, row["event_pk"], row["quarantine_path"]
        ):
            try:
                results.append(recover(conn, config, row["attempt_id"]))
            except (OSError, OperationsError) as exc:
                results.append(
                    {
                        "attempt_id": row["attempt_id"],
                        "state": "recovery_required",
                        "error": type(exc).__name__,
                    }
                )
    return results
