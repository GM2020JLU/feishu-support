from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .approvals import valid_board_lease
from .broker_storage import check_database
from .cli import DEFAULT_CONFIG
from .config import Config, load_config
from .db import migration_files
from .executors import BoardExecutor, ExecutionResult, validate_board_action
from .job_capability import CapabilityError, verified_context


class CodexBoardError(RuntimeError):
    pass


def _authorize(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    session_id: str,
) -> None:
    job_id = os.environ.get("K3_SUPPORT_JOB_ID")
    capability = os.environ.get("K3_SUPPORT_CAPABILITY")
    bound_case = os.environ.get("K3_SUPPORT_CASE_ID")
    if not job_id or not capability or bound_case != case_id:
        raise CodexBoardError("missing Case-bound Codex job capability")
    job = conn.execute(
        """SELECT j.context_json,j.lease_owner,j.lease_expires_at,j.attempt_no,c.state FROM jobs j JOIN cases c USING(case_id)
           WHERE j.job_id=? AND j.case_id=? AND j.job_type='codex' AND j.state='running'
             AND j.lifecycle_round=c.lifecycle_round""",
        (job_id, case_id),
    ).fetchone()
    if job is None or job["state"] != "board_testing":
        raise CodexBoardError("running Codex job is not in board_testing state")
    if (not job["lease_owner"] or os.environ.get("K3_SUPPORT_LEASE_OWNER") != job["lease_owner"]
            or os.environ.get("K3_SUPPORT_EXECUTION_ROUND") != str(job["attempt_no"])):
        raise CodexBoardError("Codex execution lease or attempt changed")
    try:
        expiry = datetime.fromisoformat(job["lease_expires_at"])
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise ValueError("expired")
    except (TypeError, ValueError):
        raise CodexBoardError("Codex execution lease expired or unavailable") from None
    try:
        context = verified_context(job["context_json"], capability)
    except CapabilityError as error:
        raise CodexBoardError(str(error)) from error
    expected_session = context.get("board_session_id")
    if expected_session != session_id:
        raise CodexBoardError("board session does not match immutable job context")
    if valid_board_lease(conn, case_id=case_id, session_id=session_id) is None:
        raise CodexBoardError("no valid board1 occupancy lease")


def execute_codex_board_action(
    config: Config,
    *,
    case_id: str,
    session_id: str,
    action: dict[str, Any],
    executor: BoardExecutor | None = None,
) -> ExecutionResult:
    conn = None
    try:
        identity = check_database(config.database_path)
        conn = sqlite3.connect(Path(config.database_path).absolute().as_uri()+"?mode=rw",
                               uri=True, isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if check_database(config.database_path) != identity:
            raise CodexBoardError("control database changed")
        if {(r[0], r[1]) for r in conn.execute("SELECT version,name FROM schema_migrations")} != {
                (version, name) for version, name, _ in migration_files()}:
            raise CodexBoardError("explicit control database migration required")
        authorize = lambda: _authorize(conn, case_id=case_id, session_id=session_id)
        authorize()
        return (executor or BoardExecutor(config)).execute(
            conn, case_id=case_id, session_id=session_id,
            action=validate_board_action(action), authorize=authorize)
    except (OSError, sqlite3.Error, ValueError) as error:
        raise CodexBoardError("existing private control state unavailable") from error
    finally:
        if conn is not None:
            conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="k3-codex-board")
    parser.add_argument("case_id")
    parser.add_argument("session_id")
    parser.add_argument("--action", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        action = json.loads(args.action)
    except json.JSONDecodeError as exc:
        raise CodexBoardError("board action is not valid JSON") from exc
    result = execute_codex_board_action(
        load_config(DEFAULT_CONFIG),
        case_id=args.case_id,
        session_id=args.session_id,
        action=action,
    )
    print(
        json.dumps(
            {
                "argv": result.argv,
                "output_digest": result.output_digest,
                "returncode": result.returncode,
                "stderr": result.stderr,
                "stdout": result.stdout,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
