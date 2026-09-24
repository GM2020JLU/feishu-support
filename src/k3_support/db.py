from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from .timeutil import iso_now


class DatabaseError(RuntimeError):
    pass


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        db_path.parent.chmod(0o700)
    except PermissionError:
        pass
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=FULL")
    if db_path.exists():
        try:
            db_path.chmod(0o600)
        except PermissionError:
            pass
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Composable local write unit; a savepoint never commits its caller's work."""
    if not conn.in_transaction:
        with transaction(conn):
            yield conn
        return
    name = "unit_" + uuid.uuid4().hex
    conn.execute("SAVEPOINT " + name)
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK TO " + name)
        conn.execute("RELEASE " + name)
        raise
    else:
        conn.execute("RELEASE " + name)


def migration_files() -> list[tuple[int, str, str]]:
    root = resources.files("k3_support").joinpath("migrations")
    found: list[tuple[int, str, str]] = []
    for item in root.iterdir():
        if item.name.endswith(".sql") and item.name[:3].isdigit():
            found.append((int(item.name[:3]), item.name, item.read_text(encoding="utf-8")))
    return sorted(found)


def migrate(conn: sqlite3.Connection) -> list[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    available = migration_files()
    unsupported = applied - {version for version, _, _ in available}
    if unsupported:
        raise DatabaseError(
            f"database schema is newer than this application: {sorted(unsupported)}; "
            "stop mixed-version workers and use the matching release or restore a backup"
        )
    completed: list[int] = []
    for version, name, sql in available:
        if version in applied:
            continue
        safe_name = name.replace("'", "''")
        safe_time = iso_now().replace("'", "''")
        script = (
            "BEGIN IMMEDIATE;\n"
            + sql
            + f"\nINSERT INTO schema_migrations(version,name,applied_at) "
            f"VALUES({version},'{safe_name}','{safe_time}');\nCOMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise DatabaseError(f"migration {name} failed: {exc}") from exc
        completed.append(version)
    return completed


def integrity(conn: sqlite3.Connection) -> dict[str, object]:
    quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    fk = [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]
    return {"quick_check": quick, "foreign_key_errors": fk, "ok": quick == "ok" and not fk}
