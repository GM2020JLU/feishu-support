"""Private in-memory database boundary for workflow replay.

This isolates SQLite writes, not Python callbacks, transports or filesystem
access. A replay executor must separately deny those external side effects.
"""
from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .db import migrate


class SnapshotLimitError(ValueError):
    """Snapshot preparation exceeded its configured resource budget."""


@contextmanager
def replay_snapshot(database: Path, *, max_bytes: int = 256 * 1024 * 1024,
                    timeout_seconds: float = 30, upgrade_schema: bool = True) -> Iterator[sqlite3.Connection]:
    """Copy an existing database read-only and upgrade only the memory copy.

    Never open a missing source with SQLite's create default. Keeping the copy
    in memory also avoids leaving exported chat bodies in a temporary directory.
    The caller owns execution policy; no worker or outbox consumer is started.
    upgrade_schema=False preserves a recorded schema for version-bound replay;
    it does not select historical code or establish capture-time provenance.
    """
    if type(upgrade_schema) is not bool:
        raise ValueError('upgrade_schema must be a boolean')
    if type(max_bytes) is not int or not 4096 <= max_bytes <= 1024 * 1024 * 1024:
        raise ValueError("max_bytes must be between 4096 and 1073741824")
    if (type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300):
        raise ValueError("timeout_seconds must be finite and in (0, 300]")
    deadline = time.monotonic() + timeout_seconds

    def check_deadline():
        if time.monotonic() >= deadline:
            raise SnapshotLimitError("snapshot preparation timed out")

    source = sqlite3.connect(
        database.resolve().as_uri() + "?mode=ro", uri=True,
        isolation_level=None, timeout=0,
    )
    target = None
    try:
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        if source.execute("PRAGMA page_count").fetchone()[0] * page_size > max_bytes:
            raise SnapshotLimitError("source database exceeds snapshot size limit")
        check_deadline()
        target = sqlite3.connect(":memory:", isolation_level=None)

        def progress(status, remaining, total):
            check_deadline()
            if total * page_size > max_bytes:
                raise SnapshotLimitError("source database grew beyond snapshot size limit")

        source.backup(target, pages=64, progress=progress, sleep=0.01)
        source.close()
        target.row_factory = sqlite3.Row
        target.execute("PRAGMA foreign_keys=ON")
        target.execute(f"PRAGMA max_page_count={max_bytes // page_size}")
        target.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            if upgrade_schema:
                migrate(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("replay snapshot integrity check failed")
            if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("replay snapshot has invalid references")
            check_deadline()
        except sqlite3.OperationalError as exc:
            if time.monotonic() >= deadline:
                raise SnapshotLimitError("snapshot preparation timed out") from exc
            raise
        finally:
            target.set_progress_handler(None, 0)
        yield target
    finally:
        source.close()
        if target is not None:
            target.close()
