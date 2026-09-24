"""Durable control-side observations, never execution or recovery authorization."""

import hashlib
from uuid import UUID

from .broker_process import run_process
from .broker_remote_probe import observe
from .db import transaction
from .ids import canonical_json
from .timeutil import iso_now


def _snapshot(conn, config, request_id):
    row = conn.execute("SELECT a.*,g.job_id,g.attempt_no,g.lifecycle_round,g.input_digest,g.revoked_at,"
                       "j.state AS job_state,j.attempt_no AS job_attempt,j.lifecycle_round AS job_round,"
                       "j.input_digest AS job_input,j.lease_owner,c.version AS case_version,c.lifecycle_round AS case_round "
                       "FROM broker_remote_actions a JOIN broker_grants g USING(grant_id) "
                       "JOIN jobs j USING(job_id) JOIN cases c ON c.case_id=j.case_id WHERE a.request_id=?",
                       (request_id,)).fetchone()
    if row is None:
        raise ValueError("remote request unavailable")
    return hashlib.sha256(canonical_json({"action": dict(row), "runtime": config.raw["runtime"]}).encode()).hexdigest()


def record(conn, config, *, observation_id, request_id, transport=run_process):
    for value in (observation_id, request_id):
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("canonical observation and request identifiers required")
    with transaction(conn):
        previous = conn.execute("SELECT * FROM broker_remote_observations WHERE observation_id=?", (observation_id,)).fetchone()
        if previous is not None:
            if previous["request_id"] != request_id:
                raise ValueError("observation identity changed")
            return {"observation_id": observation_id, "request_id": request_id, "state": previous["state"],
                    "historical": True, "recovery_authorized": False}
        snapshot = _snapshot(conn, config, request_id)
        conn.execute("INSERT INTO broker_remote_observations VALUES(?,?,?,'pending',NULL,?,NULL)",
                     (observation_id, request_id, snapshot, iso_now()))
    # Commit intent before network I/O; crashes remain pending, never automatic
    # replay. No write transaction is held while waiting on the remote host.
    try:
        result = observe(conn, config, request_id=request_id, transport=transport)
    except (ValueError, OSError, TypeError, KeyError):
        result = {"state": "unknown", "request_id": request_id, "recovery_authorized": False}
    with transaction(conn):
        current = conn.execute("SELECT * FROM broker_remote_observations WHERE observation_id=?", (observation_id,)).fetchone()
        if (current is None or current["state"] != "pending" or current["request_id"] != request_id
                or current["snapshot_digest"] != snapshot):
            raise ValueError("observation changed during retrieval")
        if _snapshot(conn, config, request_id) != snapshot:
            state, result = "stale", {"state": "stale", "recovery_authorized": False}
        else:
            state = "observed" if result["state"] == "guardian_returned" else "unknown"
        conn.execute("UPDATE broker_remote_observations SET state=?,result_json=?,finished_at=? WHERE observation_id=?",
                     (state, canonical_json(result), iso_now(), observation_id))
    return {"observation_id": observation_id, "request_id": request_id, "state": state,
            "historical": False, "recovery_authorized": False}


def main(argv=None):
    import argparse
    import json
    import sqlite3
    from pathlib import Path

    from .broker_storage import check_database
    from .config import load_config
    from .db import migration_files

    parser = argparse.ArgumentParser(description="Observe one remote receipt and record audit; never resume execution.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--observation-id", required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--cleanup-preview", action="store_true")
    action.add_argument("--cleanup-apply", metavar="PREVIEW_DIGEST")
    args = parser.parse_args(argv)
    conn = None
    try:
        config = load_config(args.config)
        identity = check_database(config.database_path)
        conn = sqlite3.connect(Path(config.database_path).absolute().as_uri() + "?mode=rw",
                               uri=True, isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        if check_database(config.database_path) != identity:
            raise ValueError("database changed")
        if {(r[0], r[1]) for r in conn.execute("SELECT version,name FROM schema_migrations")} != {
                (v, name) for v, name, _ in migration_files()}:
            raise ValueError("explicit database migration required")
        options = {"observation_id": args.observation_id, "request_id": args.request_id}
        if args.cleanup_preview:
            from .broker_remote_cleanup import preview
            result = preview(conn, config, **options)
        elif args.cleanup_apply is not None:
            from .broker_remote_cleanup import apply
            result = apply(conn, config, preview_digest=args.cleanup_apply, **options)
        else:
            result = record(conn, config, **options)
        print(json.dumps(result))
        return 0
    except (ValueError, OSError, TypeError, KeyError, sqlite3.Error):
        print(json.dumps({"state": "unavailable", "recovery_authorized": False}))
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
