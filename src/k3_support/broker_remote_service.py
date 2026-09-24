"""Private control-side remote queue consumer; no automatic uncertain retries."""

import argparse
import json
import os
import pwd
import signal
import sqlite3
import sys
import threading
from pathlib import Path

from .broker_execution_contract import load_at
from .broker_remote_runner import run_one
from .broker_storage import check_database
from .config import load_config
from .db import migration_files


def run(*, config_path, contract_directory, worker_uid, watch, stop_event, consumer=None, execution_catalog=False):
    if type(worker_uid) is not int or worker_uid <= 0 or worker_uid == os.geteuid():
        raise ValueError("independent worker required")
    from .project_investigation_source import monitor_source_config

    config = monitor_source_config(load_config(config_path))
    identity = check_database(config.database_path)
    directory = os.open(contract_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    conn = None
    try:
        reader = lambda: load_at(directory, control_uid=os.geteuid(), worker_uid=worker_uid)
        if not execution_catalog:
            reader()
        conn = sqlite3.connect(Path(config.database_path).absolute().as_uri() + "?mode=rw",
                               uri=True, isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if check_database(config.database_path) != identity:
            raise ValueError("database changed during startup")
        expected = {(version, name) for version, name, _ in migration_files()}
        if {(r[0], r[1]) for r in conn.execute("SELECT version,name FROM schema_migrations")} != expected:
            raise ValueError("database migration required")
        if execution_catalog:
            from .broker_catalog import ActiveCatalog, Catalog
            catalog = Catalog(directory, control_uid=os.geteuid(), worker_uid=worker_uid)
            catalog.profiles()
            reader = ActiveCatalog(conn, catalog)
        while not stop_event.is_set():
            result = (consumer or run_one)(conn, config, contract_reader=reader, stop_event=stop_event)
            if not watch:
                return result
            stop_event.wait(1)
        return {"state": "stopped"}
    finally:
        if conn is not None:
            conn.close()
        os.close(directory)


def main(argv=None, *, consumer=None, description=None):
    parser = argparse.ArgumentParser(description=description or __doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--contract-directory", required=True)
    parser.add_argument("--execution-catalog", action="store_true")
    parser.add_argument("--worker-user", required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args(argv)
    stopped = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stopped.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        result = run(config_path=args.config, contract_directory=args.contract_directory,
                     worker_uid=pwd.getpwnam(args.worker_user).pw_uid, watch=args.watch, stop_event=stopped,
                     consumer=consumer, execution_catalog=args.execution_catalog)
        if not args.watch:
            print(json.dumps(result))
    except (OSError, ValueError, KeyError, sqlite3.Error):
        print("Remote consumer unavailable; inspect control configuration and unresolved execution state.", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
