"""Independent control-account Project reader; never initializes/migrates a DB."""

import argparse
import json
import os
import pwd
import signal
import sqlite3
import stat
import threading
from pathlib import Path

from .broker_storage import check_database
from .config import load_config
from .db import migration_files
from .project_reader_config import selected
from .project_refresh import run_one as refresh_one
from .timeutil import iso_now


def run_one(conn, config_loader, *, stop_event=None):
    """Share one reader fairly across refresh, intake, search and activity."""
    from .project_activity import run_one as activity_one
    from .project_bug_search import run_one as search_one
    from .project_link_intake import run_one as intake_one

    now = iso_now()
    row = conn.execute(
        "SELECT kind FROM (SELECT 'refresh' AS kind,created_at FROM project_refresh_requests "
        "WHERE state='queued' OR (state='running' AND lease_expires_at<=?) "
        "UNION ALL SELECT 'intake' AS kind,created_at FROM project_link_intakes "
        "WHERE state='queued' OR (state='running' AND lease_expires_at<=?) "
        "UNION ALL SELECT 'search' AS kind,created_at FROM project_search_requests "
        "WHERE state='queued' OR (state='running' AND lease_expires_at<=?) "
        "UNION ALL SELECT 'activity' AS kind,created_at FROM project_activity_requests "
        "WHERE state='queued' OR (state='running' AND lease_expires_at<=?)) ORDER BY created_at,kind LIMIT 1",
        (now, now, now, now),
    ).fetchone()
    consumer = {"intake": intake_one, "search": search_one, "refresh": refresh_one, "activity": activity_one}[row[0]] if row else refresh_one
    return consumer(conn, config_loader, stop_event=stop_event)


def _private(path, directory=False):
    info = Path(path).lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("Project control storage must be owner-private")
    return info.st_dev, info.st_ino


def _protected_dir(path):
    """Config may live in a root-owned /etc directory shared with worker
    contracts; require a trusted owner and no group/other write, like the
    reader executable parents, instead of owner-private."""
    info = Path(path).lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or info.st_mode & 0o022
    ):
        raise ValueError("Project configuration directory must be protected")
    return info.st_dev, info.st_ino


def run(config_path, *, worker_uid, watch, stop_event, consumer=None):
    if type(worker_uid) is not int or worker_uid <= 0 or worker_uid == os.geteuid():
        raise ValueError("independent worker identity required")
    home = Path.home()
    if home != Path(pwd.getpwuid(os.geteuid()).pw_dir):
        raise ValueError("use the control account home")
    _private(home, True)
    _private(home / ".meegle", True)
    path = Path(config_path).absolute()
    _protected_dir(path.parent)

    def config_loader():
        _private(home, True)
        _private(home / ".meegle", True)
        _protected_dir(path.parent)
        before = _private(path)
        config = load_config(path)
        if _private(path) != before:
            raise ValueError("configuration changed while loading")
        reader = selected(config)
        if reader is not None:
            for parent in Path(reader["executable"]).parents:
                info = parent.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_mode & 0o022
                    or info.st_uid not in {0, os.geteuid()}
                ):
                    raise ValueError("Project executable parents must be protected")
        return config

    config = config_loader()
    identity = check_database(config.database_path)
    conn = sqlite3.connect(
        Path(config.database_path).absolute().as_uri() + "?mode=rw",
        uri=True,
        isolation_level=None,
        timeout=5,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        if check_database(config.database_path) != identity:
            raise ValueError("database changed while opening")
        if {
            (r[0], r[1])
            for r in conn.execute("SELECT version,name FROM schema_migrations")
        } != {(v, n) for v, n, _ in migration_files()}:
            raise ValueError("explicit database migration required")

        def fixed_database_config():
            current = config_loader()
            if (
                current.database_path != config.database_path
                or check_database(current.database_path) != identity
            ):
                raise ValueError("reader database changed")
            return current

        while not stop_event.is_set():
            result = (consumer or run_one)(conn, fixed_database_config, stop_event=stop_event)
            if not watch:
                return result
            stop_event.wait(1)
        return {"state": "stopped"}
    finally:
        conn.close()


def main(argv=None, *, consumer=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--worker-user", required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args(argv)
    stopped = threading.Event()
    previous = {
        s: signal.signal(s, lambda *_: stopped.set())
        for s in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        result = run(
            args.config,
            worker_uid=pwd.getpwnam(args.worker_user).pw_uid,
            watch=args.watch,
            stop_event=stopped,
            consumer=consumer,
        )
        if not args.watch:
            print(json.dumps(result))
    except (OSError, ValueError, KeyError, sqlite3.Error):
        print(json.dumps({"error": "project_reader_unavailable"}))
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
