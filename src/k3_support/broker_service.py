"""Control broker entry point; never migrates or starts workers."""

import argparse
import ctypes
import grp
import os
import pwd
import signal
import sqlite3
import sys
import threading
from contextlib import ExitStack
from pathlib import Path

from .broker_activation import take_listener
from .broker_execution_contract import load_at
from .broker_key import load_key_at
from .broker_listener import serve
from .broker_storage import check_database
from .broker_unit_references import RetainedUnitObservations as UnitReferences
from .config import load_config
from .db import migration_files


def run(*, config_path, key_directory, worker_uid, stop_event, contract_directory=None, execution_catalog=False, listen_path=None, socket_group=None):
    from .project_investigation_source import monitor_source_config

    config = monitor_source_config(load_config(config_path))
    if type(worker_uid) is not int or not 0 < worker_uid < 4294967295 or worker_uid == os.geteuid():
        raise ValueError("independent worker UID required")
    with ExitStack() as stack:
        contract_reader = None
        if contract_directory is not None:
            contract_fd = os.open(contract_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            stack.callback(os.close, contract_fd)
            contract_reader = lambda: load_at(contract_fd, control_uid=os.geteuid(), worker_uid=worker_uid)
            if not execution_catalog:
                contract_reader()
        key_fd = os.open(key_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stack.callback(os.close, key_fd)
        key = load_key_at(key_fd)
        identity = check_database(config.database_path)
        # rw forbids silent creation; deployment still owns the ancestor paths
        # and must separately verify worker access to all backups and credentials.
        database = Path(config.database_path).absolute().as_uri() + "?mode=rw"
        conn = sqlite3.connect(database, uri=True, isolation_level=None, timeout=5)
        stack.callback(conn.close)
        if check_database(config.database_path) != identity:
            raise ValueError("database changed during startup")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        actual = {(row[0], row[1]) for row in conn.execute("SELECT version,name FROM schema_migrations")}
        expected = {(version, name) for version, name, _ in migration_files()}
        if actual != expected:
            raise ValueError("database migration required before broker startup")
        if execution_catalog:
            from .broker_catalog import ActiveCatalog, Catalog
            if contract_directory is None:
                raise ValueError("catalog directory required")
            catalog = Catalog(contract_fd, control_uid=os.geteuid(), worker_uid=worker_uid)
            catalog.profiles()
            contract_reader = ActiveCatalog(conn, catalog)
        if listen_path is not None:
            from .broker_owned_listener import owned_listener
            listener = stack.enter_context(owned_listener(listen_path, group_id=grp.getgrnam(socket_group).gr_gid))
        else:
            listener = stack.enter_context(take_listener())
        references = UnitReferences()
        stack.callback(references.close)
        if listen_path is not None and os.environ.get("NOTIFY_SOCKET"):
            notify = ctypes.CDLL("libsystemd.so.0").sd_notify
            notify.argtypes = [ctypes.c_int, ctypes.c_char_p]
            notify.restype = ctypes.c_int
            if notify(0, b"READY=1") <= 0:
                raise ValueError("broker readiness notification failed")
        return serve(conn, listener, config=config, worker_uid=worker_uid, control_key=key,
                     stop_event=stop_event, unit_references=references, contract_reader=contract_reader)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--key-directory", required=True)
    parser.add_argument("--contract-directory", required=True)
    parser.add_argument("--execution-catalog", action="store_true")
    parser.add_argument("--listen-path")
    parser.add_argument("--socket-group")
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--worker-uid", type=int)
    identity.add_argument("--worker-user")
    args = parser.parse_args(argv)
    if bool(args.listen_path) != bool(args.socket_group):
        parser.error("listen path and socket group must be provided together")
    stopped = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stopped.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        worker_uid = pwd.getpwnam(args.worker_user).pw_uid if args.worker_user else args.worker_uid
        run(config_path=args.config, key_directory=args.key_directory, worker_uid=worker_uid, stop_event=stopped,
            contract_directory=args.contract_directory, execution_catalog=args.execution_catalog,
            listen_path=args.listen_path, socket_group=args.socket_group)
    except (OSError, ValueError, KeyError, sqlite3.Error):
        print("Broker unavailable: check activation, private key, database schema and worker identity.", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
