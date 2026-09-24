from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from .cli import DEFAULT_CONFIG
from .config import Config, load_config
from .execution_transport import command_argv
from .job_capability import CapabilityError, verified_context
from .remote_sandbox import render_remote_command, sandbox_argv


class RemoteSandboxError(RuntimeError):
    pass


ACTIVE_CODEX_STATES = {
    "triage",
    "investigating",
    "waiting_board",
    "board_testing",
    "waiting_push",
    "monitoring",
}


def _case_allows_work(conn: sqlite3.Connection, case_id: str) -> None:
    row = conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None or row["state"] not in ACTIVE_CODEX_STATES:
        raise RemoteSandboxError("Case is missing or no longer permits delegated work")
    job_id = os.environ.get("K3_SUPPORT_JOB_ID")
    capability = os.environ.get("K3_SUPPORT_CAPABILITY")
    bound_case = os.environ.get("K3_SUPPORT_CASE_ID")
    if not job_id or not capability or bound_case != case_id:
        raise RemoteSandboxError("missing Case-bound Codex job capability")
    job = conn.execute(
        """SELECT j.context_json,j.lease_owner,j.lease_expires_at,j.attempt_no FROM jobs j JOIN cases c USING(case_id)
           WHERE j.job_id=? AND j.case_id=? AND j.lifecycle_round=c.lifecycle_round
           AND j.job_type='codex' AND j.state='running'""",
        (job_id, case_id),
    ).fetchone()
    if job is None:
        raise RemoteSandboxError("no running Codex job owns this Case capability")
    if (not job["lease_owner"] or os.environ.get("K3_SUPPORT_LEASE_OWNER") != job["lease_owner"]
            or os.environ.get("K3_SUPPORT_EXECUTION_ROUND") != str(job["attempt_no"])):
        raise RemoteSandboxError("Codex execution lease or attempt changed")
    try:
        expiry = datetime.fromisoformat(job["lease_expires_at"])
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise ValueError("expired")
    except (TypeError, ValueError):
        raise RemoteSandboxError("Codex execution lease expired or unavailable") from None
    try:
        verified_context(job["context_json"], capability)
    except CapabilityError as error:
        raise RemoteSandboxError(str(error)) from error


def build_remote_command(
    config: Config,
    *,
    case_id: str,
    mode: str,
    repo_name: str | None,
    command: str,
    work_id: str | None = None,
    seed_work_id: str | None = None,
    seed_work_ids: list[str] | None = None,
) -> str:
    if mode not in {"inspect", "work"}:
        raise RemoteSandboxError("mode must be inspect or work")
    if not command or "\x00" in command or len(command.encode()) > 32768:
        raise RemoteSandboxError("remote command is empty or too large")
    if mode == "work":
        repo = config.raw["repositories"].get(repo_name)
        if not isinstance(repo, dict):
            raise RemoteSandboxError("work mode requires one configured repository")
    elif repo_name is not None:
        raise RemoteSandboxError("inspect mode does not accept a repository")
    try:
        argv = sandbox_argv(
            case_id=case_id,
            source_root=config.runtime("remote_source_root"),
            worktree_root=config.runtime("remote_worktree_root"),
            repo_paths=[item["path"] for item in config.raw["repositories"].values()],
            toolchain_roots=config.raw["runtime"].get("remote_toolchain_roots", []),
            writable=mode == "work",
            command=command,
            work_id=work_id,
            seed_work_id=seed_work_id,
            seed_work_ids=seed_work_ids,
        )
        return render_remote_command(argv, writable=mode == "work", nested_work=work_id is not None)
    except ValueError as exc:
        raise RemoteSandboxError(str(exc)) from exc


def _check_control(config, case_id):
    # A command wrapper is not a deployment/migration entrypoint. Missing or
    # incompatible control state must fail without creating files or changing ACLs.
    conn = None
    try:
        conn = sqlite3.connect(Path(config.database_path).absolute().as_uri() + "?mode=ro",
                               uri=True, isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        _case_allows_work(conn, case_id)
    except sqlite3.Error:
        raise RemoteSandboxError("existing control state unavailable; deployment must prepare it") from None
    finally:
        if conn is not None:
            conn.close()


def _run_supervised(argv, *, heartbeat, timeout=7200):
    heartbeat()
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + timeout
        while True:
            heartbeat()
            if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                break
            if time.monotonic() >= deadline:
                raise RemoteSandboxError("SSH deadline exceeded; remote execution requires reconciliation")
            time.sleep(0.25)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            process.wait(timeout=5)
    return process.returncode


def run_remote(
    config: Config,
    *,
    case_id: str,
    mode: str,
    repo_name: str | None,
    command: str,
) -> int:
    _check_control(config, case_id)
    remote = build_remote_command(
        config,
        case_id=case_id,
        mode=mode,
        repo_name=repo_name,
        command=command,
    )
    return _run_supervised(
        command_argv(config, remote),
        heartbeat=lambda: _check_control(config, case_id),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="k3-codex-remote")
    parser.add_argument("case_id")
    sub = parser.add_subparsers(dest="mode", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("command", nargs=argparse.REMAINDER)
    work = sub.add_parser("work")
    work.add_argument("--repo", required=True)
    work.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command: list[str] = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if len(command) != 1:
        raise RemoteSandboxError("pass exactly one remote shell command after --")
    config = load_config(DEFAULT_CONFIG)
    return run_remote(
        config,
        case_id=args.case_id,
        mode=args.mode,
        repo_name=getattr(args, "repo", None),
        command=command[0],
    )


if __name__ == "__main__":
    raise SystemExit(main())
