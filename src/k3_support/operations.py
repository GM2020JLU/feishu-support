from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .db import integrity, transaction
from .delivery_attempts import record_outcome
from .ids import canonical_json, new_id
from .store import recover_stale_jobs
from .timeutil import iso_now, parse_iso


class OperationsError(RuntimeError):
    pass


def heartbeat(
    conn: sqlite3.Connection, component: str, status: str, detail: dict[str, Any]
) -> None:
    now = iso_now()
    conn.execute(
        """INSERT INTO service_state(component,pid,started_at,heartbeat_at,status,detail_json)
           VALUES(?,?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET pid=excluded.pid,
           heartbeat_at=excluded.heartbeat_at,status=excluded.status,detail_json=excluded.detail_json""",
        (component, os.getpid(), now, now, status, canonical_json(detail)),
    )


def backup_database(config: Config, *, now: datetime | None = None, timeout_seconds: float = 120) -> dict[str, Any]:
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600):
        raise OperationsError('backup timeout must be finite and in (0, 3600] seconds')
    deadline = time.monotonic() + timeout_seconds

    def check_progress(*_args):
        if time.monotonic() >= deadline:
            raise OperationsError('backup deadline exceeded; no snapshot published')

    observed = now or datetime.now(UTC)
    if not isinstance(observed, datetime) or observed.tzinfo is None or observed.utcoffset() is None:
        raise OperationsError('backup timestamp must be timezone-aware')
    observed = observed.astimezone(UTC)
    database = config.database_path
    if not database.is_file() or any(path.is_symlink() for path in (database, *database.parents)):
        raise OperationsError('backup requires an existing regular source database without symlink ancestors')
    backup_dir = config.data_dir / "backups"
    if any(path.is_symlink() for path in (backup_dir, *backup_dir.parents)):
        raise OperationsError('backup destination directory and ancestors must not be symlinks')
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f"support-{observed.strftime('%Y%m%dT%H%M%SZ')}.db"
    target = backup_dir / name
    if target.exists() or target.is_symlink():
        raise OperationsError("backup target already exists")
    fd, staging_name = tempfile.mkstemp(prefix='.backup-pending-', suffix='.db', dir=backup_dir)
    os.close(fd)
    staging = Path(staging_name)
    try:
        source = sqlite3.connect(database.absolute().as_uri() + '?mode=ro', uri=True, timeout=0)
        try:
            destination = sqlite3.connect(staging)
            try:
                source.backup(destination, pages=128, progress=check_progress, sleep=0.05)
                if destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise OperationsError("backup could not be finalized as a standalone snapshot")
            finally:
                destination.close()
        finally:
            source.close()
        check_conn = sqlite3.connect(staging.absolute().as_uri() + '?mode=ro', uri=True)
        check_conn.row_factory = sqlite3.Row
        try:
            check = integrity(check_conn)
        finally:
            check_conn.close()
        if not check['ok']:
            raise OperationsError('backup integrity check failed')
        with staging.open('rb') as stream:
            file_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
            os.fsync(stream.fileno())
        size = staging.stat().st_size
        check_progress()
        # Same-directory hard-link publication is atomic and refuses an existing
        # destination. A failed/incomplete snapshot never gets a support-*.db name.
        os.link(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return {
        "path": str(target),
        "size": size,
        "sha256": file_hash,
        "integrity": check,
    }


def prune_backups(
    config: Config, *, keep_daily: int = 14, keep_weekly: int = 8, dry_run: bool = False,
    max_age_days: int | None = None, now: datetime | None = None,
) -> list[str]:
    if type(dry_run) is not bool:
        raise OperationsError('dry_run must be boolean')
    if (type(keep_daily) is not int or not 1 <= keep_daily <= 3650
            or type(keep_weekly) is not int or not 0 <= keep_weekly <= 520):
        raise OperationsError('invalid backup retention counts; retain at least one recent backup')
    if max_age_days is not None and (type(max_age_days) is not int or not 1 <= max_age_days <= 3650):
        raise OperationsError('backup max age must be null or 1..3650 days')
    observed = now or datetime.now(UTC)
    if not isinstance(observed, datetime) or observed.tzinfo is None or observed.utcoffset() is None:
        raise OperationsError('backup retention timestamp must be timezone-aware')
    observed = observed.astimezone(UTC)
    backup_dir = config.data_dir / "backups"
    if any(parent.is_symlink() for parent in (backup_dir, *backup_dir.parents)):
        raise OperationsError('backup directory and ancestors must not be symlinks')
    if not backup_dir.exists():
        return []
    files = sorted(
        [
            path
            for path in backup_dir.glob("support-*.db")
            if path.is_file() and not path.is_symlink()
        ],
        key=lambda path: path.name,
        reverse=True,
    )
    stamps = {path: path.stat(follow_symlinks=False) for path in files}
    keep = set()
    recent_count = 0
    newest_preserved = False
    weekly: set[tuple[int, int]] = set()
    for path in files:
        try:
            stamp = datetime.strptime(path.name, "support-%Y%m%dT%H%M%SZ.db").replace(
                tzinfo=UTC
            )
        except ValueError:
            # Unknown files are not ours to delete, even if the prefix matches.
            keep.add(path)
            continue
        # Future timestamps cannot evict the latest non-future recovery point.
        if stamp > observed:
            keep.add(path)
            continue
        if not newest_preserved:
            keep.add(path)
            newest_preserved = True
        if max_age_days is not None and stamp <= observed - timedelta(days=max_age_days):
            continue
        if recent_count < keep_daily:
            keep.add(path)
            recent_count += 1
        week = stamp.isocalendar()[:2]
        if week not in weekly and len(weekly) < keep_weekly:
            weekly.add(week)
            keep.add(path)
    removed: list[str] = []
    for path in files:
        if path in keep:
            continue
        if not dry_run:
            from .retention_recovery import _parent

            # Reopen without following any directory symlink at the deletion
            # boundary; unlink relative to that pinned directory descriptor.
            parent_fd, name = _parent(Path(os.path.abspath(path)))
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode):
                    raise OperationsError('backup target is no longer a regular file')
                original = stamps[path]
                if ((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                        != (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns)):
                    raise OperationsError('backup target changed during retention selection')
                from .backup_prune_audit import deletion_receipt

                with deletion_receipt(parent_fd, name=name, observed=current,
                                      policy={'keep_recent': keep_daily, 'keep_weekly': keep_weekly,
                                              'max_age_days': max_age_days, 'observed_at': observed.isoformat()}):
                    # Recheck after audit fsync: do not unlink a replacement
                    # which appeared while durable intent was being recorded.
                    latest = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if ((latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns, latest.st_ctime_ns)
                            != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)):
                        raise OperationsError('backup changed after prune intent')
                    os.unlink(name, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
        removed.append(str(path))
    return removed


def restore_probe(backup_path: str | Path) -> dict[str, Any]:
    from .retention_recovery import _parent, _signature

    # This is an offline, standalone backup probe, not a live WAL reader.
    # Walk without resolving symlinks and pin the file before SQLite opens it.
    path = Path(os.path.abspath(Path(backup_path).expanduser()))
    parent_fd = file_fd = None
    try:
        parent_fd, name = _parent(path)
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat(name + suffix, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise OperationsError("backup must be standalone without SQLite sidecars")
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise OperationsError("backup is not a regular file")
        conn = sqlite3.connect(f"file:/proc/self/fd/{file_fd}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            check = integrity(conn)
            counts = {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("cases", "case_events", "approvals", "outbox")
            }
        finally:
            conn.close()
        if (_signature(before) != _signature(os.fstat(file_fd))
                or _signature(before) != _signature(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))):
            raise OperationsError("backup changed during restore probe")
    except (OSError, sqlite3.Error) as exc:
        raise OperationsError("backup cannot be safely inspected") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)
    if not check["ok"]:
        raise OperationsError("restore probe failed integrity")
    return {"path": str(path), "integrity": check, "counts": counts}


def _safe_artifact(config: Config, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise OperationsError("artifact path must be absolute")
    resolved = candidate.resolve(strict=False)
    allowed_roots = [
        (config.data_dir / name).resolve() for name in ("attachments", "cases", "logs")
    ]
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise OperationsError("artifact path escapes retention roots")
    if any(part.is_symlink() for part in (candidate, *candidate.parents)):
        raise OperationsError("artifact path is a symlink")
    return candidate


def _artifact_stamp(path: Path) -> list[int] | None:
    try:
        value = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise OperationsError("artifact is not a private regular file")
    return [
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    ]


_RETENTION_HASH_MAX_BYTES = 64 * 1024 * 1024


def _draft_hash_referenced(conn, path):
    if not conn.execute('SELECT 1 FROM knowledge_authoring_drafts LIMIT 1').fetchone():
        return False
    # An unverified source hash can only retain data, never grant publication.
    # Unreadable, changing or oversized files stay held for explicit mapping.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _RETENTION_HASH_MAX_BYTES:
                return True
            checksum = hashlib.sha256()
            total = 0
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > _RETENTION_HASH_MAX_BYTES:
                    return True
                checksum.update(chunk)
            after = os.fstat(stream.fileno())
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if identity(before) != identity(after) or identity(after) != identity(path.stat(follow_symlinks=False)):
                return True
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return conn.execute(
        "SELECT 1 FROM knowledge_authoring_drafts WHERE json_extract(material_json,'$.source.sha256')=? LIMIT 1",
        (checksum.hexdigest(),),
    ).fetchone() is not None


def _retention_referenced(conn, event_pk, path):
    # Human takeover is still ongoing work. Knowledge provenance is retained
    # even when its Case is closed; a retired answer can still have receipts.
    has_drafts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_authoring_drafts'").fetchone()
    # Captured drafts are deliberately outside knowledge_entries. They still
    # depend on their original source; recheck this at mutation time as well
    # as preview, so a newly saved draft cannot lose its raw evidence.
    if has_drafts and conn.execute(
            """SELECT 1 FROM knowledge_authoring_drafts d, inbound_events i
               WHERE i.event_pk=? AND (
                 NOT json_valid(d.material_json) OR
                 CASE WHEN json_valid(d.material_json)
                   THEN json_extract(d.material_json,'$.source.id') END
                   IN (i.event_pk,i.external_id,?)) LIMIT 1""",
            (event_pk, str(path)),
        ).fetchone():
        return True
    referenced = (
        conn.execute(
            """SELECT 1 FROM case_events ce JOIN cases c USING(case_id)
           WHERE ce.source_event_pk=? AND (c.state NOT IN ('resolved','cancelled')
             OR EXISTS(SELECT 1 FROM knowledge_entries k WHERE k.canonical_case_id=c.case_id))
           UNION ALL
           SELECT 1 FROM inbound_events i JOIN knowledge_sources ks
             ON ks.stable_external_id IN (i.event_pk,i.external_id)
             OR ks.url=i.raw_artifact_path WHERE i.event_pk=?
           UNION ALL
           SELECT 1 FROM inbound_events WHERE raw_artifact_path=? AND event_pk<>?
           LIMIT 1""",
            (event_pk, event_pk, str(path), event_pk),
        ).fetchone()
        is not None
    )
    return referenced or bool(has_drafts and _draft_hash_referenced(conn, path))


def retention_preview(
    conn: sqlite3.Connection, config: Config, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    cutoff = (now or datetime.now(UTC)) - timedelta(
        days=int(config.raw["policy"]["raw_retention_days"])
    )
    rows = conn.execute(
        """SELECT raw_artifact_path,received_at,event_pk FROM inbound_events
           WHERE raw_artifact_path IS NOT NULL AND received_at<?
             AND NOT EXISTS(SELECT 1 FROM retention_attempts r
               WHERE r.event_pk=inbound_events.event_pk AND r.state IN ('prepared','quarantined'))""",
        (cutoff.isoformat(),),
    ).fetchall()
    preview = []
    for row in rows:
        path = _safe_artifact(config, row["raw_artifact_path"])
        active = _retention_referenced(conn, row["event_pk"], path)
        stamp = _artifact_stamp(path)
        preview.append(
            {
                "path": str(path),
                "event_pk": row["event_pk"],
                "status": "held"
                if active
                else ("delete" if path.exists() else "missing"),
                "bytes": path.stat().st_size if path.is_file() else 0,
                "file_stamp": stamp,
                "received_at": row["received_at"],
            }
        )
    return preview


def apply_retention(
    conn: sqlite3.Connection, config: Config, preview: list[dict[str, Any]]
) -> dict[str, int]:
    from .retention_recovery import prepare, quarantine

    counts = {"deleted": 0, "quarantined": 0, "held": 0, "missing": 0, "failed": 0}
    for item in preview:
        path = _safe_artifact(config, item["path"])
        if item["status"] != "delete":
            counts[item["status"]] += 1
            continue
        if not item.get("file_stamp") or not item.get("received_at"):
            counts["held"] += 1
            continue
        attempt = prepare(conn, config, item)
        if attempt["state"] != "prepared":
            counts["held"] += 1
            continue
        try:
            with transaction(conn):
                state = conn.execute(
                    "SELECT state FROM retention_attempts WHERE attempt_id=?",
                    (attempt["attempt_id"],),
                ).fetchone()
                if state is None or state[0] != "prepared":
                    counts["held"] += 1
                    continue
                current = conn.execute(
                    "SELECT raw_artifact_path,received_at FROM inbound_events WHERE event_pk=?",
                    (item["event_pk"],),
                ).fetchone()
                cutoff = datetime.now(UTC) - timedelta(
                    days=int(config.raw["policy"]["raw_retention_days"])
                )
                if (
                    current is None
                    or current["raw_artifact_path"] != str(path)
                    or current["received_at"] != item.get("received_at")
                    or parse_iso(current["received_at"]) >= cutoff
                    or not item.get("file_stamp")
                    or _artifact_stamp(path) != item["file_stamp"]
                    or _retention_referenced(conn, item["event_pk"], path)
                ):
                    counts["held"] += 1
                    conn.execute(
                        "UPDATE retention_attempts SET state='cancelled',reason='current_input_or_reference_changed',updated_at=? WHERE attempt_id=?",
                        (iso_now(), attempt["attempt_id"]),
                    )
                    continue
                quarantine(conn, config, attempt)
                conn.execute(
                    """INSERT INTO retention_tombstones(tombstone_id,artifact_type,artifact_path,bytes_removed,
                           status,reason,created_at) VALUES(?,'inbound_raw',?,0,'held',?,?)""",
                    (
                        new_id("tmb"),
                        str(path),
                        "recoverable_quarantine:" + attempt["attempt_id"],
                        iso_now(),
                    ),
                )
            counts["quarantined"] += 1
        except (OSError, OperationsError, sqlite3.Error, ValueError):
            # The prepared intent was committed before mutation. Recovery can
            # restore a file even when this transaction or its commit fails.
            counts["failed"] += 1
    return counts


def reconcile(
    conn: sqlite3.Connection, *, config: Config | None = None
) -> dict[str, Any]:
    retention_recovery = []
    if config is not None:
        from .retention_recovery import reconcile_retention

        retention_recovery = reconcile_retention(conn, config)
    now = iso_now()
    stale_jobs = recover_stale_jobs(conn, now)
    with transaction(conn):
        from .mail import (
            commit_summary_delivery,
            complete_mail_outbox_delivery,
            fail_mail_outbox,
        )

        reclaimed_inbox = conn.execute(
            """UPDATE inbound_events SET status='new',lease_owner=NULL,lease_expires_at=NULL
               WHERE status='claimed' AND lease_expires_at IS NOT NULL AND lease_expires_at<?""",
            (now,),
        ).rowcount
        expired_attempts = conn.execute(
            """SELECT o.*,a.dispatch_started_at AS attempt_started_at
               FROM outbox o JOIN outbox_attempts a ON a.claim_token=o.claim_token
               WHERE o.state='sending' AND o.lease_expires_at<?""",
            (now,),
        ).fetchall()
        for attempt in expired_attempts:
            record_outcome(
                conn,
                dict(attempt),
                event_type="lease_expired",
                detail={"may_have_started": attempt["attempt_started_at"] is not None},
            )
        # A late transport receipt is authoritative only for its own attempt.
        # Do not revive cancellations or copy it onto a newer claim.
        confirmed = conn.execute(
            """SELECT o.outbox_id,o.claim_token,e.remote_message_id,e.detail_json,e.recorded_at
               FROM outbox o JOIN outbox_attempt_events e ON e.claim_token=o.claim_token
               WHERE e.event_type='delivered' AND e.remote_message_id IS NOT NULL
                 AND ((o.state='sending' AND o.lease_expires_at<?)
                   OR (o.state='permanent_failure' AND (
                     json_extract(o.remote_result_json,'$.error_type')='uncertain_delivery'
                     OR EXISTS(SELECT 1 FROM outbox_attempt_events uncertain
                         WHERE uncertain.claim_token=o.claim_token AND uncertain.event_type='uncertain'))))""",
            (now,),
        ).fetchall()
        recovered_known_receipts = 0
        for receipt in confirmed:
            recovered_known_receipts += conn.execute(
                """UPDATE outbox SET state='delivered',lease_owner=NULL,lease_expires_at=NULL,
                     remote_message_id=?,remote_result_json=?,delivered_at=?,updated_at=?
                   WHERE outbox_id=? AND claim_token=? AND state IN ('sending','permanent_failure')""",
                (
                    receipt["remote_message_id"],
                    receipt["detail_json"],
                    receipt["recorded_at"],
                    now,
                    receipt["outbox_id"],
                    receipt["claim_token"],
                ),
            ).rowcount
        # New attempts with a durable proof that dispatch never began are safe
        # to requeue even for a non-idempotent channel. NULL legacy tokens are
        # deliberately excluded because old code left no such proof.
        reclaimed_unstarted = conn.execute(
            """UPDATE outbox SET state='retry',lease_owner=NULL,lease_expires_at=NULL,
                   next_attempt_at=?,updated_at=?
               WHERE state='sending' AND lease_expires_at<? AND remote_message_id IS NULL
                 AND EXISTS(SELECT 1 FROM outbox_attempts a
                   WHERE a.claim_token=outbox.claim_token AND a.dispatch_started_at IS NULL)""",
            (now, now, now),
        ).rowcount
        finalized_receipts = 0
        if config is not None:
            from .delivery import DeliveryReceipt, finalize_delivery_effects

            unfinished = conn.execute(
                """SELECT * FROM outbox WHERE state='delivered' AND claim_token IS NOT NULL
                     AND effects_finalized_at IS NULL AND remote_message_id IS NOT NULL"""
            ).fetchall()
            for delivered in unfinished:
                finalize_delivery_effects(
                    conn,
                    config,
                    dict(delivered),
                    DeliveryReceipt(
                        str(delivered["remote_message_id"]),
                        json.loads(delivered["remote_result_json"] or "{}"),
                    ),
                    delivered_at=str(delivered["delivered_at"] or now),
                )
                finalized_receipts += 1
        recovered_delivered = (
            conn.execute(
                """UPDATE outbox SET state='delivered',lease_owner=NULL,lease_expires_at=NULL,
               delivered_at=coalesce(delivered_at,?),updated_at=?
               WHERE state='sending' AND lease_expires_at<? AND remote_message_id IS NOT NULL""",
                (now, now, now),
            ).rowcount
            + recovered_known_receipts
        )
        recovered_mail_links = 0
        delivered_mail = conn.execute(
            """SELECT outbox_id,action_type,remote_message_id,remote_result_json
                 FROM outbox WHERE channel='mail' AND state='delivered'
                   AND claim_token IS NULL
                   AND remote_message_id IS NOT NULL"""
        ).fetchall()
        for mail_row in delivered_mail:
            linked = conn.execute(
                """SELECT state FROM mail_digest_links
                   WHERE share_outbox_id=? OR resolve_outbox_id=?""",
                (mail_row["outbox_id"], mail_row["outbox_id"]),
            ).fetchone()
            expected = (
                "shared" if mail_row["action_type"] == "share_to_owner" else "delivered"
            )
            if (
                linked is None
                or linked["state"] == expected
                or linked["state"] == "failed"
            ):
                continue
            try:
                result = json.loads(mail_row["remote_result_json"] or "{}")
            except json.JSONDecodeError:
                result = {}
            complete_mail_outbox_delivery(
                conn,
                outbox_id=str(mail_row["outbox_id"]),
                action_type=str(mail_row["action_type"]),
                remote_message_id=str(mail_row["remote_message_id"]),
                result=result,
            )
            recovered_mail_links += 1
        recovered_summary_watermarks = 0
        summaries = conn.execute(
            """SELECT sr.outbox_id,o.remote_message_id
                 FROM summary_runs sr JOIN outbox o USING(outbox_id)
                WHERE sr.state='prepared' AND o.state='delivered'
                  AND o.claim_token IS NULL
                  AND o.remote_message_id IS NOT NULL"""
        ).fetchall()
        for summary in summaries:
            commit_summary_delivery(
                conn,
                outbox_id=str(summary["outbox_id"]),
                remote_message_id=str(summary["remote_message_id"]),
            )
            recovered_summary_watermarks += 1
        reclaimed_outbox = (
            conn.execute(
                """UPDATE outbox SET state='retry',lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=?
               WHERE state='sending' AND lease_expires_at<? AND remote_message_id IS NULL
               AND (channel='feishu_im' OR (channel='mail' AND action_type='resolve_app_link'))""",
                (now, now),
            ).rowcount
            + reclaimed_unstarted
        )
        uncertain_mail_rows = conn.execute(
            """SELECT outbox_id,action_type FROM outbox
               WHERE state='sending' AND lease_expires_at<? AND remote_message_id IS NULL
                 AND channel='mail' AND action_type='share_to_owner'""",
            (now,),
        ).fetchall()
        uncertain_outbox = conn.execute(
            """UPDATE outbox SET state='permanent_failure',lease_owner=NULL,lease_expires_at=NULL,
               remote_result_json=?,updated_at=?
               WHERE state='sending' AND lease_expires_at<? AND remote_message_id IS NULL
               AND channel<>'feishu_im'
               AND NOT (channel='mail' AND action_type='resolve_app_link')""",
            (
                canonical_json(
                    {
                        "error_type": "uncertain_delivery",
                        "message": "sender crashed after a non-idempotent call may have started; not retried",
                    }
                ),
                now,
                now,
            ),
        ).rowcount
        for mail_row in uncertain_mail_rows:
            fail_mail_outbox(
                conn,
                outbox_id=str(mail_row["outbox_id"]),
                action_type=str(mail_row["action_type"]),
                error="uncertain_delivery: sender crashed after mail sharing may have started",
            )
        expired_approvals = conn.execute(
            """UPDATE approvals SET status='expired',updated_at=?
               WHERE status IN ('requested','approved') AND expires_at<?""",
            (now, now),
        ).rowcount
        expired_board_locks = conn.execute(
            "SELECT count(*) FROM locks WHERE lock_key='board1' AND expires_at<?",
            (now,),
        ).fetchone()[0]
        recovered_delivery_blocks = 0
        recovered_context_rechecks = 0
        if config is not None:
            from .context_recovery import reconcile_context_rechecks
            from .delivery_recovery import reconcile_delivery_blocks

            recovered_delivery_blocks = reconcile_delivery_blocks(conn, config)
            recovered_context_rechecks = reconcile_context_rechecks(conn, config)
    return {
        "recovered_jobs": stale_jobs["recovered"],
        "retention_recovery": retention_recovery,
        "orphaned_jobs": stale_jobs["orphaned"],
        "reclaimed_inbox": reclaimed_inbox,
        "reclaimed_outbox": reclaimed_outbox,
        "recovered_delivered": recovered_delivered,
        "finalized_receipts": finalized_receipts,
        "recovered_mail_links": recovered_mail_links,
        "recovered_summary_watermarks": recovered_summary_watermarks,
        "uncertain_outbox": uncertain_outbox,
        "expired_approvals": expired_approvals,
        "expired_board_locks": expired_board_locks,
        "recovered_delivery_blocks": recovered_delivery_blocks,
        "recovered_context_rechecks": recovered_context_rechecks,
        "integrity": integrity(conn),
    }
