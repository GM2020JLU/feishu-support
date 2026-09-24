"""Quiet evidence collector; worker dispatch requires an explicit opt-in."""

import argparse
import json
import os
import pwd
import signal
import sqlite3
import sys
import threading
from pathlib import Path

from .broker_completion import reconcile
from .broker_storage import check_database
from .broker_systemd_observer import observe_exit, observe_instance
from .config import load_config
from .db import migration_files


def sweep(conn, *, after="", stop_event=None):
    # A bounded, rotating keyset prevents old unavailable services starving newer
    # tasks. Missing historical instances remain unverified, never backfilled.
    rows = conn.execute("""SELECT s.grant_id,c.request_id,i.grant_id AS registered
        FROM broker_execution_starts s
        JOIN jobs j ON j.job_id=s.job_id
        JOIN broker_claim_receipts c ON c.peer_uid=s.peer_uid
          AND json_extract(c.binding_json,'$.job_id')=s.job_id
          AND json_extract(c.binding_json,'$.execution_round')=s.attempt_no
        LEFT JOIN broker_execution_instances i ON i.grant_id=s.grant_id
        LEFT JOIN broker_service_exits e ON e.grant_id=s.grant_id
        WHERE (e.grant_id IS NULL OR (j.state IN ('running','succeeded') AND j.attempt_no=s.attempt_no AND NOT EXISTS (
            SELECT 1 FROM case_events v WHERE v.idempotency_key='job:'||s.job_id||':recorded'
        ))) AND s.grant_id>?
        ORDER BY s.grant_id LIMIT 4""", (after,)).fetchall()
    result = {"observed": 0, "unverified": 0, "after": after}
    if not rows:
        result["after"] = ""
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            break
        result["after"] = row["grant_id"]
        try:
            if row["registered"]:
                if not conn.execute("SELECT 1 FROM broker_service_exits WHERE grant_id=?", (row["grant_id"],)).fetchone():
                    observe_exit(conn, grant_id=row["grant_id"])
                reconcile(conn, grant_id=row["grant_id"])
            else:
                observe_instance(conn, grant_id=row["grant_id"], claim_request_id=row["request_id"])
            result["observed"] += 1
        except ValueError:
            # Unavailable/running/mismatched observations are not exit evidence.
            # Do not log task contents, command output, or recurring alerts.
            result["unverified"] += 1
    if stop_event is None or not stop_event.is_set():
        from .broker_dispatch import finish_observed, reclaim_unstarted_launches

        finish_observed(conn)
        reclaim_unstarted_launches(conn)
    return result


def run(*, config_path, watch, stop_event, dispatch_workers=False, contract_directory=None, worker_uid=None,
        launcher_socket=None, execution_catalog=False):
    if dispatch_workers and (not contract_directory or type(worker_uid) is not int
                             or worker_uid <= 0 or worker_uid == os.geteuid()):
        raise ValueError("dispatch requires a trusted contract and independent worker")
    config = load_config(config_path)
    identity = check_database(config.database_path)
    conn = sqlite3.connect(Path(config.database_path).absolute().as_uri() + "?mode=rw",
                           uri=True, isolation_level=None, timeout=5)
    contract_fd = None
    try:
        if dispatch_workers:
            from .broker_execution_contract import load_at

            contract_fd = os.open(contract_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            contract_reader = lambda: load_at(contract_fd, control_uid=os.geteuid(), worker_uid=worker_uid)
            if execution_catalog:
                from .broker_catalog import ActiveCatalog, Catalog
                catalog = Catalog(contract_fd, control_uid=os.geteuid(), worker_uid=worker_uid)
                catalog.profiles()
                contract_reader = ActiveCatalog(conn, catalog)
            else:
                contract_reader()
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if check_database(config.database_path) != identity:
            raise ValueError("database changed during observer startup")
        expected = {(version, name) for version, name, _ in migration_files()}
        if {(r[0], r[1]) for r in conn.execute("SELECT version,name FROM schema_migrations")} != expected:
            raise ValueError("database migration required")
        cursor = ""
        while not stop_event.is_set():
            result = sweep(conn, after=cursor, stop_event=stop_event)
            if dispatch_workers and not stop_event.is_set():
                from .broker_dispatch import dispatch_one

                options = {}
                if launcher_socket is not None:
                    from .broker_launcher import request

                    options["launch"] = lambda request_id: request(launcher_socket, request_id)
                result["dispatch"] = dispatch_one(conn, config, contract_reader=contract_reader, **options)
            if not watch:
                return result
            cursor = result["after"]
            stop_event.wait(5)
    finally:
        if contract_fd is not None:
            os.close(contract_fd)
        conn.close()
    return {"stopped": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--dispatch-workers", action="store_true")
    parser.add_argument("--contract-directory")
    parser.add_argument("--execution-catalog", action="store_true")
    parser.add_argument("--worker-user")
    parser.add_argument("--launcher-socket")
    args = parser.parse_args(argv)
    if args.dispatch_workers and (not args.contract_directory or not args.worker_user or not args.launcher_socket):
        parser.error("--dispatch-workers requires contract, worker and launcher socket settings")
    if not args.dispatch_workers and (args.contract_directory or args.worker_user or args.launcher_socket or args.execution_catalog):
        parser.error("worker settings require --dispatch-workers")
    stopped = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stopped.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        uid = pwd.getpwnam(args.worker_user).pw_uid if args.dispatch_workers else None
        result = run(config_path=args.config, watch=args.watch, stop_event=stopped,
                     dispatch_workers=args.dispatch_workers, contract_directory=args.contract_directory, worker_uid=uid,
                     launcher_socket=args.launcher_socket, execution_catalog=args.execution_catalog)
        if not args.watch:
            print(json.dumps(result))
    except (OSError, ValueError, KeyError, sqlite3.Error):
        print("Observer unavailable: verify private database and service manager access.", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
