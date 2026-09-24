from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from .approvals import (
    ApprovalError,
    valid_board_lease,
    valid_push_approval,
)
from .config import Config
from .db import transaction
from .evidence import requester_access_for_case
from .execution_transport import command_argv
from .ids import canonical_json, digest, new_id
from .store import EXECUTABLE_CASE_STATES
from .timeutil import iso_now


class ExecutorError(RuntimeError):
    pass


class RetrievalHandoffSuperseded(ExecutorError):
    """The retrieval authority changed before its child could be queued."""


@dataclass(frozen=True)
class ExecutionResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def output_digest(self) -> str:
        return hashlib.sha256((self.stdout + "\0" + self.stderr).encode()).hexdigest()


Runner = Callable[[list[str], str | None, int], ExecutionResult]
CODEX_RESULT_SECTIONS = (
    "status",
    "root_cause",
    "changes",
    "verification",
    "board_state",
    "push_state",
    "artifacts",
    "risks",
    "next_action",
    "reply_draft",
)


def run_process(
    argv: list[str], cwd: str | None = None, timeout: int = 3600
) -> ExecutionResult:
    process = subprocess.run(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return ExecutionResult(argv, process.returncode, process.stdout, process.stderr)


def _process_start_token(pid: int) -> str:
    if type(pid) is not int or pid <= 0:
        return "unavailable"
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # comm (field 2) is parenthesized and may itself contain spaces or ')'.
        # Fields after its final ')' begin at field 3; starttime is field 22.
        if not raw.startswith(f"{pid} (") or ")" not in raw:
            return "unavailable"
        fields = raw.rsplit(")", 1)[1].split()
        token = fields[19]
        return token if token.isascii() and token.isdigit() else "unavailable"
    except (IndexError, OSError, UnicodeError):
        return "unavailable"


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        # The owned child/group may have exited between poll and killpg.
        # Still reap the child with a bound; do not swallow permission errors.
        process.wait(timeout=5)
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _drain_stopped_process(process):
    """Do not wait forever for inherited pipes held by escaped descendants."""
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired as exc:
        def text(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
        return text(exc.output), (text(exc.stderr) + "\nOutput drain timed out; descendant exit is unverified").strip()
    finally:
        for name in ("stdout", "stderr"):
            stream = getattr(process, name, None)
            if stream is not None:
                stream.close()


def _run_codex_supervised(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    argv: list[str],
    cwd: str,
    timeout: int,
    should_stop: Callable[[], bool],
    codex_home: Path,
) -> ExecutionResult:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    job = conn.execute(
        "SELECT case_id,context_json,lease_owner,attempt_no,lifecycle_round FROM jobs WHERE job_id=? AND state='running'",
        (job_id,),
    ).fetchone()
    if job is None:
        raise ExecutorError("running Codex job disappeared before process start")
    def execution_permitted():
        # Cancellation and lease replacement must stop the process, not merely
        # reject its eventual output. An unreadable authority is not permission.
        try:
            current = conn.execute(
                """SELECT j.state,j.lease_owner,j.attempt_no,j.lifecycle_round,
                          c.state AS case_state,c.lifecycle_round AS case_round
                     FROM jobs j JOIN cases c USING(case_id) WHERE j.job_id=?""",
                (job_id,),
            ).fetchone()
            return bool(current and current["state"] == "running"
                        and current["lease_owner"] == job["lease_owner"]
                        and current["attempt_no"] == job["attempt_no"]
                        and current["lifecycle_round"] == job["lifecycle_round"]
                        and current["case_round"] == job["lifecycle_round"]
                        and current["case_state"] in EXECUTABLE_CASE_STATES
                        and not should_stop())
        except Exception:  # noqa: BLE001 - failed authority checks stop execution
            return False
    capability_path = Path(cwd) / ".capability"
    if not capability_path.is_file() or capability_path.is_symlink():
        raise ExecutorError("Codex job capability is unavailable")
    capability = capability_path.read_text(encoding="utf-8").strip()
    from .job_capability import CapabilityError, verified_context

    try:
        verified_context(job["context_json"], capability)
    except CapabilityError as error:
        raise ExecutorError("Codex job capability does not match its immutable context") from error
    environment["K3_SUPPORT_JOB_ID"] = job_id
    environment["K3_SUPPORT_LEASE_OWNER"] = str(job["lease_owner"])
    environment["K3_SUPPORT_EXECUTION_ROUND"] = str(job["attempt_no"])
    environment["K3_SUPPORT_CASE_ID"] = str(job["case_id"])
    environment["K3_SUPPORT_CAPABILITY"] = capability
    if not execution_permitted():
        raise ExecutorError("Codex execution authority changed before process start")
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=environment,
    )
    try:
        return _monitor_codex_process(
            conn, job_id=job_id, job=job, argv=argv, process=process,
            timeout=timeout, execution_permitted=execution_permitted,
        )
    finally:
        # A failed PID/heartbeat/receipt write must not abandon a live child.
        if process.poll() is None:
            _stop_process_group(process)
            _drain_stopped_process(process)


def _monitor_codex_process(conn, *, job_id, job, argv, process, timeout, execution_permitted):
    process_start_token = _process_start_token(process.pid)
    now = datetime.now(UTC)
    with transaction(conn):
        registered = conn.execute(
            "UPDATE jobs SET pid=?,process_start_token=?,heartbeat_at=?,lease_expires_at=?,updated_at=? "
            "WHERE job_id=? AND state='running' AND attempt_no=? AND lease_owner IS ? AND lifecycle_round=?",
            (
                process.pid,
                process_start_token,
                now.isoformat(),
                (now + timedelta(seconds=120)).isoformat(),
                now.isoformat(),
                job_id,
                job["attempt_no"],
                job["lease_owner"],
                job["lifecycle_round"],
            ),
        )
        if registered.rowcount != 1:
            raise ExecutorError("Codex execution identity changed before PID registration")
    deadline = time.monotonic() + timeout
    next_heartbeat = time.monotonic() + 30
    stdout = ""
    stderr = ""
    returncode: int | None = None
    while returncode is None:
        if not execution_permitted():
            _stop_process_group(process)
            stdout, stderr = _drain_stopped_process(process)
            returncode = 143
            stderr = (stderr + "\nCodex worker stopped before completion").strip()
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_process_group(process)
            stdout, stderr = _drain_stopped_process(process)
            returncode = 124
            stderr = (stderr + "\nCodex job exceeded its supervised timeout").strip()
            break
        try:
            stdout, stderr = process.communicate(timeout=min(2, remaining))
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            if time.monotonic() < next_heartbeat:
                continue
            next_heartbeat = time.monotonic() + 30
            heartbeat_at = datetime.now(UTC)
            with transaction(conn):
                conn.execute(
                    "UPDATE jobs SET heartbeat_at=?,lease_expires_at=?,updated_at=? "
                    "WHERE job_id=? AND state='running' AND pid=? AND process_start_token=? "
                    "AND attempt_no=? AND lease_owner IS ? AND lifecycle_round=?",
                    (
                        heartbeat_at.isoformat(),
                        (heartbeat_at + timedelta(seconds=120)).isoformat(),
                        heartbeat_at.isoformat(),
                        job_id,
                        process.pid,
                        process_start_token,
                        job["attempt_no"],
                        job["lease_owner"],
                        job["lifecycle_round"],
                    ),
                )
    actual_returncode = getattr(process, "returncode", None)
    if type(actual_returncode) is int:
        conn.execute(
            "INSERT INTO execution_exit_receipts VALUES(?,?,?,?,?,?,?,?)",
            (new_id("exit"), job_id, job["attempt_no"], job["lease_owner"], process.pid,
             process_start_token, actual_returncode, iso_now()),
        )
    return ExecutionResult(argv, int(returncode), stdout or "", stderr or "")


def _case_dir(config: Config, case_id: str) -> Path:
    if not re.fullmatch(r"K3-\d{8}-\d{4}", case_id):
        raise ExecutorError("invalid case ID")
    root = config.data_dir / "cases"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / case_id
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _render_codex_policy(config: Config) -> str:
    def rule(pattern: list[str], decision: str, justification: str) -> str:
        return (
            "prefix_rule("
            f"pattern = {json.dumps(pattern, ensure_ascii=True)}, "
            f'decision = "{decision}", '
            f"justification = {json.dumps(justification, ensure_ascii=True)})"
        )

    forbidden: list[tuple[list[str], str]] = [
        (["git", "push"], "Use the exact-digest WIP push executor."),
        (["ssh"], "Use the isolated K3 remote wrapper."),
        (["/usr/bin/ssh"], "Use the isolated K3 remote wrapper."),
        (["/bin/ssh"], "Use the isolated K3 remote wrapper."),
        (["scp"], "Direct remote transfer is not allowed."),
        (["sftp"], "Direct remote transfer is not allowed."),
        (["rsync"], "Direct remote transfer is not allowed."),
        (["serial"], "The test board requires a verified lease."),
        (["fastboot"], "The test board requires a verified lease."),
        (["lark-cli"], "Codex must not send messages."),
        (["hermes"], "Codex must not control Hermes."),
        (["k3-supportctl"], "Codex must not alter control state."),
        (["curl"], "Delegated shell network is disabled."),
        (["wget"], "Delegated shell network is disabled."),
        (["nc"], "Delegated shell network is disabled."),
        (["socat"], "Delegated shell network is disabled."),
    ]
    configured_forbidden = [
        ([config.runtime("ssh_command")], "Use the isolated K3 remote wrapper."),
        (
            [config.runtime("serial_command")],
            "The test board requires a verified lease.",
        ),
        (
            ["bash", config.runtime("board_control_script")],
            "Use the Case-bound board wrapper.",
        ),
        (
            [config.runtime("board_control_script")],
            "Use the Case-bound board wrapper.",
        ),
        (
            ["bash", config.runtime("board_boot_script")],
            "Use the Case-bound board wrapper.",
        ),
        (
            [config.runtime("board_boot_script")],
            "Use the Case-bound board wrapper.",
        ),
    ]
    seen: set[tuple[str, ...]] = set()
    lines: list[str] = []
    for pattern, justification in [*forbidden, *configured_forbidden]:
        key = tuple(pattern)
        if key in seen:
            continue
        seen.add(key)
        lines.append(rule(pattern, "forbidden", justification))
    lines.extend(
        (
            rule(
                [config.runtime("codex_remote_command")],
                "allow",
                "Case-scoped, repository-scoped, network-isolated build-host access.",
            ),
            rule(
                [config.runtime("codex_board_command")],
                "allow",
                "Case capability and board lease are revalidated by the wrapper.",
            ),
        )
    )
    return "\n".join(lines) + "\n"


def _prepare_codex_home(config: Config) -> Path:
    home = config.data_dir / "codex-home"
    rules = home / "rules"
    rules.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    rules.chmod(0o700)
    source_home = Path.home() / ".codex"
    for name in ("auth.json", "config.toml"):
        source = source_home / name
        target = home / name
        if not source.is_file():
            raise ExecutorError(f"Codex {name} is unavailable")
        if target.exists() or target.is_symlink():
            if not target.is_symlink() or target.resolve() != source.resolve():
                raise ExecutorError(f"isolated Codex {name} has an unexpected target")
        else:
            target.symlink_to(source)
    policy = _render_codex_policy(config)
    policy_path = rules / "k3-support.rules"
    if policy_path.is_symlink():
        raise ExecutorError("isolated Codex policy path is a symlink")
    policy_path.write_text(policy, encoding="utf-8")
    policy_path.chmod(0o600)
    return home


def validate_codex_result_text(value: str) -> dict[str, str]:
    if not value.strip() or len(value.encode()) > 2 * 1024 * 1024:
        raise ExecutorError("Codex result is empty or exceeds 2 MiB")
    names = "|".join(re.escape(name) for name in CODEX_RESULT_SECTIONS)
    headings = list(
        re.finditer(
            rf"(?im)^\s*(?:#{{1,6}}\s*)?({names})\s*:?\s*$",
            value,
        )
    )
    sections: dict[str, str] = {}
    for index, match in enumerate(headings):
        key = match.group(1).lower()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(value)
        content = value[match.end() : end].strip()
        if key in sections:
            raise ExecutorError(f"Codex result repeats section {key}")
        sections[key] = content
    missing = [name for name in CODEX_RESULT_SECTIONS if not sections.get(name)]
    if missing:
        raise ExecutorError(
            f"Codex result is missing non-empty sections: {', '.join(missing)}"
        )
    status = sections["status"].splitlines()[0].strip().lower()
    if status not in {"completed", "partial", "blocked", "failed"}:
        raise ExecutorError(
            "Codex status must be completed, partial, blocked, or failed"
        )
    sections["status"] = status
    return sections


def record_codex_result(
    conn: sqlite3.Connection,
    *,
    job_id: str,
) -> dict[str, str]:
    row = conn.execute(
        "SELECT case_id,workdir,state,output_digest FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if row is None or row["state"] != "succeeded":
        raise ExecutorError("only a succeeded Codex job can be recorded")
    from .codex_result_source import read_result

    sections = validate_codex_result_text(read_result(conn, job_id=job_id).decode("utf-8"))
    case_id = str(row["case_id"])
    now = iso_now()
    with transaction(conn):
        existing = conn.execute(
            "SELECT 1 FROM case_events WHERE idempotency_key=?",
            (f"job:{job_id}:recorded",),
        ).fetchone()
        if existing:
            return sections
        conn.execute(
            """INSERT INTO case_suggestions(suggestion_id,case_id,kind,content_json,confidence,
                   policy_version,status,created_at) VALUES(?,?,'reply_draft',?,?,?,'shadow',?)""",
            (
                new_id("sug"),
                case_id,
                canonical_json({"job_id": job_id, "sections": sections}),
                0.0,
                "v1",
                now,
            ),
        )
        case = conn.execute(
            "SELECT version FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        sequence = conn.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO case_events(event_id,case_id,sequence,event_type,actor_type,actor_id,
                   before_state,after_state,detail_json,idempotency_key,created_at,created_epoch)
               SELECT ?,?,?,'codex_completed','codex',?,state,state,?,?,?,?
               FROM cases WHERE case_id=?""",
            (
                new_id("cev"),
                case_id,
                sequence,
                job_id,
                canonical_json(
                    {
                        "job_id": job_id,
                        "output_digest": row["output_digest"],
                        "status": sections["status"],
                    }
                ),
                f"job:{job_id}:recorded",
                now,
                int(datetime.now(UTC).timestamp()),
                case_id,
            ),
        )
        conn.execute(
            "UPDATE cases SET next_action=?,updated_at=? WHERE case_id=? AND version=?",
            (sections["next_action"][:1000], now, case_id, case["version"]),
        )
    return sections


def create_codex_job(
    conn: sqlite3.Connection,
    config: Config,
    *,
    case_id: str,
    brief: str,
    repo: str | list[str],
    context_extra: dict[str, Any] | None = None,
    retrieval_parent_id: str | None = None,
    retrieval_attempt_no: int | None = None,
    execution_contract=None,
    expected_case_version: int | None = None,
) -> tuple[str, bool]:
    from .broker_execution_contract import ExecutionContract
    if execution_contract is not None and type(execution_contract) is not ExecutionContract:
        raise ExecutorError("trusted coding execution contract required")
    agent = execution_contract.agent if execution_contract else "codex"
    model = execution_contract.model if execution_contract else "gpt-5.6-sol"
    reasoning = execution_contract.reasoning if execution_contract else "medium"
    if not config.feature("codex"):
        raise ExecutorError("Codex feature is disabled")
    from .runtime_control import capability_allowed

    if not capability_allowed(conn, config, "codex"):
        raise ExecutorError("Codex is disabled by the global runtime mode")
    repos = [repo] if isinstance(repo, str) else repo
    if (
        not repos
        or len(repos) != len(set(repos))
        or any(name not in config.raw["repositories"] for name in repos)
    ):
        raise ExecutorError("every repository must be configured exactly once")
    if (
        "UNTRUSTED INPUT" not in brief
        or "FORBIDDEN ACTIONS" not in brief
        or "ACCEPTANCE TESTS" not in brief
    ):
        raise ExecutorError(
            "brief is missing trust, forbidden-action, or acceptance-test sections"
        )
    context_extra = dict(context_extra or {})
    investigation = context_extra.get("project_investigation") or {}
    joint_verification = isinstance(investigation, dict) and isinstance(investigation.get("verification"), dict) and "sources" in investigation["verification"]
    ordered_repos = list(repos) if joint_verification else sorted(repos)
    fixed_context_fields = {
        "agent",
        "capability_sha256",
        "case_root",
        "model",
        "reasoning",
        "repositories",
        "lifecycle_round",
        "execution",
    }
    if fixed_context_fields & set(context_extra):
        raise ExecutorError("Codex context_extra attempts to replace a fixed field")
    if execution_contract is not None:
        context_extra["execution"] = execution_contract.selection()
    if "project_investigation" not in context_extra and conn.execute(
        "SELECT 1 FROM project_bugs WHERE case_id=?", (case_id,)
    ).fetchone():
        raise ExecutorError("Use the Bug investigation entry point for this Case")
    case = conn.execute(
        "SELECT state,owner,lifecycle_round,version FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    if case is None:
        raise ExecutorError("Case does not exist")
    if expected_case_version is not None and case["version"] != expected_case_version:
        raise ExecutorError("Case version changed; refresh before creating a coding task")
    if case["state"] in {"resolved", "cancelled", "takeover", "paused"} or (
        case["lifecycle_round"] > 1 and case["owner"] != "hermes"
    ):
        raise ExecutorError(
            "Case round requires explicit delegation before creating a Codex job"
        )
    round_number = int(case["lifecycle_round"])
    from .content_retirement import ContentRetiredError, require_case_content
    try:
        require_case_content(conn, case_id=case_id, lifecycle_round=round_number)
    except ContentRetiredError as error:
        raise ExecutorError('Case content retired; new source material is required') from error
    input_snapshot = {
            "case_id": case_id,
            "lifecycle_round": round_number,
            "repos": ordered_repos,
            "brief": brief,
            "model": model,
            "reasoning": reasoning,
            "context_extra": context_extra,
        }
    brief_digest = digest(input_snapshot)
    job_id = new_id("job")
    now = iso_now()
    case_dir = _case_dir(config, case_id)
    job_dir = case_dir / "jobs" / job_id
    job_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    job_dir.chmod(0o700)
    brief_path = job_dir / "brief.md"
    capability_path = job_dir / ".capability"
    capability = secrets.token_urlsafe(32)
    # This file is an internal artifact; reject symlinks before writing.
    if brief_path.is_symlink():
        raise ExecutorError("brief path is a symlink")
    brief_path.write_text(brief, encoding="utf-8")
    brief_path.chmod(0o600)
    capability_path.write_text(capability, encoding="utf-8")
    capability_path.chmod(0o600)
    with transaction(conn):
        if retrieval_parent_id is not None:
            from .retrieval import validate_retrieval_binding

            valid, reason = validate_retrieval_binding(
                conn, retrieval_parent_id, require_ai=True
            )
            parent = conn.execute(
                "SELECT case_id,attempt_no FROM jobs WHERE job_id=? AND job_type='retrieve'",
                (retrieval_parent_id,),
            ).fetchone()
            if (not valid or parent is None or parent["case_id"] != case_id
                or (retrieval_attempt_no is not None and parent['attempt_no'] != retrieval_attempt_no)):
                brief_path.unlink(missing_ok=True)
                capability_path.unlink(missing_ok=True)
                job_dir.rmdir()
                raise RetrievalHandoffSuperseded(
                    f"retrieval input changed before Codex job creation: {reason}"
                )
        live_case = conn.execute(
            "SELECT state,owner,lifecycle_round,version FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if live_case is None or tuple(live_case) != tuple(case):
            brief_path.unlink(missing_ok=True)
            capability_path.unlink(missing_ok=True)
            job_dir.rmdir()
            raise ExecutorError("Case authority changed before Codex job creation")
        project_context = context_extra.get("project_investigation")
        if project_context is None and conn.execute(
            "SELECT 1 FROM project_bugs WHERE case_id=?", (case_id,)
        ).fetchone():
            brief_path.unlink(missing_ok=True)
            capability_path.unlink(missing_ok=True)
            job_dir.rmdir()
            raise ExecutorError("Use the Bug investigation entry point for this Case")
        if project_context is not None:
            from .project_investigation import validate

            try:
                validate(conn, project_context, case_id, config=config)
                from .project_investigation_candidate import (
                    resolve as resolve_candidate,
                )
                from .project_investigation_source import require_current

                if "verification" in project_context:
                    from .project_verifier_job import plan_binding
                    from .project_verifier_repository_set import source_map

                    plan_binding(conn, project_context["verification"], round_id=project_context["round_id"], source=project_context["source"], repository=repos[0])
                    sources = source_map(project_context["verification"], project_context["source"], repos[0])
                    if repos != [repos[0]] + sorted(set(sources) - {repos[0]}):
                        raise ExecutorError("verification repositories differ from the selected sources")
                else:
                    if len(repos) != 1:
                        raise ExecutorError("investigation requires one explicitly selected source")
                    sources = {repos[0]: project_context["source"]}
                for repository, source in sources.items():
                    require_current(config, repository, source)
                    resolve_candidate(conn, config, case_id=case_id, repository=repository, source=source)
            except Exception:
                brief_path.unlink(missing_ok=True)
                capability_path.unlink(missing_ok=True)
                job_dir.rmdir()
                raise
        cursor = conn.execute(
            """INSERT OR IGNORE INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,
                   workdir,context_json,created_at,updated_at)
               VALUES(?,?,'codex','queued',?,?,?,?,?,?)""",
            (
                job_id,
                case_id,
                brief_digest,
                now,
                str(job_dir),
                canonical_json(
                    {
                        "agent": agent,
                        "capability_sha256": hashlib.sha256(
                            capability.encode()
                        ).hexdigest(),
                        "case_root": str(case_dir),
                        "model": model,
                        "reasoning": reasoning,
                        "repositories": ordered_repos,
                        "lifecycle_round": round_number,
                        **context_extra,
                    }
                ),
                now,
                now,
            ),
        )
        if cursor.rowcount == 0:
            brief_path.unlink(missing_ok=True)
            capability_path.unlink(missing_ok=True)
            job_dir.rmdir()
            row = conn.execute(
                "SELECT job_id FROM jobs WHERE case_id=? AND job_type='codex' AND input_digest=?",
                (case_id, brief_digest),
            ).fetchone()
            if row is None:
                raise ExecutorError("coding request ID already used for different content")
            return str(row[0]), False
        conn.execute("INSERT INTO broker_inputs(job_id,payload_json,created_at) VALUES(?,?,?)",
                     (job_id, canonical_json(input_snapshot), now))
        if project_context is not None:
            from .project_investigation import record

            try:
                record(conn, project_context, job_id)
            except Exception:
                brief_path.unlink(missing_ok=True)
                capability_path.unlink(missing_ok=True)
                job_dir.rmdir()
                raise
    return job_id, True


def run_codex_job(
    conn: sqlite3.Connection,
    config: Config,
    *,
    job_id: str,
    runner: Runner = run_process,
    should_stop: Callable[[], bool] = lambda: False,
) -> ExecutionResult:
    from .runtime_control import capability_allowed

    if not capability_allowed(conn, config, "codex"):
        raise ExecutorError("Codex is disabled by the global runtime mode")
    requested_stop = should_stop

    def should_stop() -> bool:
        return requested_stop() or not capability_allowed(conn, config, "codex")

    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None or row["job_type"] != "codex":
        raise ExecutorError("Codex job not found")
    if "project_investigation" in json.loads(row["context_json"]):
        raise ExecutorError("Bug investigations require the scoped broker executor")
    context = json.loads(row["context_json"])
    if "execution" in context or context.get("agent", "codex") != "codex":
        raise ExecutorError("contract-bound coding tasks require the broker worker")
    if row["state"] not in {"queued", "running"}:
        raise ExecutorError(f"Codex job is {row['state']}")
    case = conn.execute(
        "SELECT state FROM cases WHERE case_id=?", (row["case_id"],)
    ).fetchone()
    if case is None or case["state"] not in EXECUTABLE_CASE_STATES:
        raise ExecutorError("Case state does not permit Codex execution")
    now_dt = datetime.now(UTC)
    with transaction(conn):
        if row["state"] == "queued":
            claimed = conn.execute(
                """UPDATE jobs SET state='running',attempt_no=attempt_no+1,lease_owner=?,
                   lease_expires_at=?,heartbeat_at=?,updated_at=? WHERE job_id=? AND state='queued'""",
                (
                    f"cli:{os.getpid()}",
                    (now_dt + timedelta(seconds=120)).isoformat(),
                    now_dt.isoformat(),
                    now_dt.isoformat(),
                    job_id,
                ),
            )
            if claimed.rowcount != 1:
                raise ExecutorError("Codex job was claimed or cancelled concurrently")
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        conn.execute(
            """INSERT OR IGNORE INTO job_attempts(attempt_id,job_id,attempt_no,started_at,worker_id)
               VALUES(?,?,?,?,?)""",
            (
                new_id("jat"),
                job_id,
                row["attempt_no"],
                now_dt.isoformat(),
                row["lease_owner"] or "unknown",
            ),
        )
    workdir = Path(row["workdir"])
    brief = (workdir / "brief.md").read_text(encoding="utf-8")
    output = workdir / "codex-final.md"
    argv = [
        "codex",
        "exec",
        "-m",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="medium"',
        "-c",
        'approval_policy="never"',
        "-s",
        "workspace-write",
        "--skip-git-repo-check",
        "--thread-source",
        "tool",
        "-C",
        str(workdir),
        "-o",
        str(output),
        brief,
    ]
    def launch():
        return (
            _run_codex_supervised(
                conn,
                job_id=job_id,
                argv=argv,
                cwd=str(workdir),
                timeout=7200,
                should_stop=should_stop,
                codex_home=_prepare_codex_home(config),
            )
            if runner is run_process
            else runner(argv, str(workdir), 7200)
        )
    try:
        from .coding_budget import execute as budgeted_coding
        from .model_budget import BudgetError

        try:
            result = budgeted_coding(
                conn, job=row, argv=argv,
                config_path=Path.home() / ".codex" / "config.toml", transport=launch,
            )
        except BudgetError as exc:
            raise ExecutorError(str(exc)) from None
    finally:
        capability_path = workdir / ".capability"
        if capability_path.is_file() and not capability_path.is_symlink():
            capability_path.unlink()
    output_exists = (
        output.is_file() if runner is run_process else result.returncode == 0
    )
    result_valid = True
    invalid_result_error: str | None = None
    if result.returncode == 0 and output_exists and runner is run_process:
        try:
            validate_codex_result_text(output.read_text(encoding="utf-8"))
        except ExecutorError as exc:
            result_valid = False
            invalid_result_error = str(exc)
    state = (
        "succeeded"
        if result.returncode == 0 and output_exists and result_valid
        else ("orphaned" if result.returncode == 143 else "failed")
    )
    error_class = None
    if state != "succeeded":
        error_class = (
            "worker_stopped"
            if state == "orphaned"
            else (
                "invalid_result"
                if invalid_result_error
                else ("missing_result" if result.returncode == 0 else "agent_exit")
            )
        )
    finished = iso_now()
    stale_reason: str | None = None
    with transaction(conn):
        current = conn.execute(
            """SELECT j.state,j.lease_owner,c.state AS case_state
                 FROM jobs j JOIN cases c USING(case_id) WHERE j.job_id=?""",
            (job_id,),
        ).fetchone()
        if (
            current is None
            or current["state"] != "running"
            or current["lease_owner"] != row["lease_owner"]
            or current["case_state"] not in EXECUTABLE_CASE_STATES
        ):
            stale_reason = "Codex job was cancelled or lost its lease while running"
            conn.execute(
                """UPDATE job_attempts SET ended_at=?,result='cancelled',detail_json=?
                   WHERE job_id=? AND attempt_no=?""",
                (
                    finished,
                    canonical_json(
                        {
                            "exit_code": result.returncode,
                            "output_digest": result.output_digest,
                            "reason": "stale_execution_fenced",
                        }
                    ),
                    job_id,
                    row["attempt_no"],
                ),
            )
        else:
            updated = conn.execute(
                """UPDATE jobs SET state=?,output_digest=?,exit_code=?,error_class=?,lease_owner=NULL,
                       lease_expires_at=NULL,updated_at=?
                     WHERE job_id=? AND state='running' AND lease_owner=?""",
                (
                    state,
                    result.output_digest,
                    result.returncode,
                    error_class,
                    finished,
                    job_id,
                    row["lease_owner"],
                ),
            )
            if updated.rowcount != 1:
                stale_reason = "Codex job lost its lease before completion"
            else:
                conn.execute(
                    """UPDATE job_attempts SET ended_at=?,result=?,detail_json=?
                       WHERE job_id=? AND attempt_no=?""",
                    (
                        finished,
                        state,
                        canonical_json(
                            {
                                "exit_code": result.returncode,
                                "output_digest": result.output_digest,
                                "validation_error": invalid_result_error,
                            }
                        ),
                        job_id,
                        row["attempt_no"],
                    ),
                )
    if stale_reason is not None:
        raise ExecutorError(stale_reason)
    if state != "succeeded":
        raise ExecutorError(f"Codex job failed: {state}")
    return result


BOARD_ACTIONS = {
    "list",
    "reset",
    "enter_brom",
    "ram_boot",
    "serial_wait",
    "serial_exec",
}


def validate_board_action(action: Any) -> dict[str, Any]:
    if not isinstance(action, dict) or action.get("type") not in BOARD_ACTIONS:
        raise ExecutorError("board action is not allowlisted")
    kind = action["type"]
    fields = set(action)
    if kind in {"list", "reset", "enter_brom"}:
        if fields != {"type"}:
            raise ExecutorError(f"{kind} has unexpected fields")
    elif kind == "ram_boot":
        if not fields <= {"type", "uboot_only", "timeout"}:
            raise ExecutorError("ram_boot has unexpected fields")
        if "uboot_only" in action and not isinstance(action["uboot_only"], bool):
            raise ExecutorError("ram_boot uboot_only must be boolean")
        if "timeout" in action and (
            not isinstance(action["timeout"], int) or not 30 <= action["timeout"] <= 900
        ):
            raise ExecutorError("ram_boot timeout must be between 30 and 900 seconds")
    elif kind == "serial_wait":
        if fields != {"type", "regex", "timeout"}:
            raise ExecutorError("serial_wait fields do not match schema")
        if not isinstance(action["regex"], str) or not 1 <= len(action["regex"]) <= 512:
            raise ExecutorError("serial_wait regex length is invalid")
        if not isinstance(action["timeout"], int) or not 1 <= action["timeout"] <= 600:
            raise ExecutorError("serial_wait timeout must be between 1 and 600 seconds")
    else:
        if fields != {"type", "command", "expect", "timeout"}:
            raise ExecutorError("serial_exec fields do not match schema")
        command = action["command"]
        if (
            not isinstance(command, list)
            or not 1 <= len(command) <= 64
            or any(
                not isinstance(item, str)
                or not item
                or "\x00" in item
                or len(item) > 1024
                for item in command
            )
        ):
            raise ExecutorError("serial_exec command is invalid")
        if (
            not isinstance(action["expect"], str)
            or not 1 <= len(action["expect"]) <= 512
        ):
            raise ExecutorError("serial_exec completion regex length is invalid")
        if not isinstance(action["timeout"], int) or not 1 <= action["timeout"] <= 300:
            raise ExecutorError("serial_exec timeout must be between 1 and 300 seconds")
    return action


class BoardExecutor:
    def __init__(self, config: Config, *, runner: Runner = run_process):
        self.config = config
        self.runner = runner
        self.board_alias = config.raw["policy"]["board_alias"]
        self.board_script = config.runtime("board_control_script")
        self.boot_script = config.runtime("board_boot_script")
        self.serial_command = config.runtime("serial_command")

    def _argv(self, action: dict[str, Any]) -> tuple[list[str], int]:
        action = validate_board_action(action)
        kind = action["type"]
        if kind == "list":
            return ["bash", self.board_script, "list"], 30
        if kind == "reset":
            return ["bash", self.board_script, self.board_alias, "reset"], 30
        if kind == "enter_brom":
            return ["bash", self.board_script, self.board_alias, "fastboot"], 45
        if kind == "ram_boot":
            argv = ["bash", self.boot_script]
            if action.get("uboot_only"):
                argv.append("--uboot-only")
            argv.extend(["--serial-alias", self.board_alias])
            return argv, int(action.get("timeout", 300))
        if kind == "serial_wait":
            regex = action.get("regex")
            if not isinstance(regex, str) or not regex:
                raise ExecutorError("serial_wait needs a regex")
            timeout = max(1, min(int(action.get("timeout", 30)), 600))
            return [
                self.serial_command,
                "wait",
                self.board_alias,
                "--after",
                "now",
                "--regex",
                regex,
                "--timeout",
                str(timeout),
                "--json",
            ], timeout + 5
        command = action.get("command")
        expect = action.get("expect")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) for item in command)
        ):
            raise ExecutorError("serial_exec needs an argv list")
        if not isinstance(expect, str) or not expect:
            raise ExecutorError("serial_exec needs a completion regex")
        timeout = max(1, min(int(action.get("timeout", 30)), 300))
        return [
            self.serial_command,
            "exec",
            self.board_alias,
            "--eol",
            "cr",
            "--pace-ms",
            "8",
            "--wait",
            expect,
            "--timeout",
            str(timeout),
            "--json",
            "--",
            *command,
        ], timeout + 5

    def execute(
        self, conn: sqlite3.Connection, *, case_id: str, session_id: str,
        action: dict[str, Any], cleanup: bool = False, cleanup_attempt_id: str | None = None,
        authorize=None, operation_id: str | None = None,
    ) -> ExecutionResult:
        from .board_operation_guard import hold

        with hold(self.config):
            if conn.execute(
                "SELECT 1 FROM broker_board_cleanup WHERE state IN ('running','unknown') LIMIT 1"
            ).fetchone():
                raise ExecutorError("unresolved broker cleanup prevents board action")
            if authorize is not None:
                authorize()
            return self._execute_locked(conn, case_id=case_id, session_id=session_id,
                                        action=action, cleanup=cleanup, cleanup_attempt_id=cleanup_attempt_id, operation_id=operation_id)

    def _execute_locked(
        self,
        conn: sqlite3.Connection,
        *,
        case_id: str,
        session_id: str,
        action: dict[str, Any],
        cleanup: bool = False,
        cleanup_attempt_id: str | None = None,
        operation_id: str | None = None,
        result_runner: Runner | None = None,
    ) -> ExecutionResult:
        if not self.config.feature("board") and not cleanup:
            raise ExecutorError("board feature is disabled")
        if not cleanup:
            from .runtime_control import capability_allowed

            if not capability_allowed(conn, self.config, "board"):
                raise ExecutorError(
                    "board execution is disabled by the global runtime mode"
                )
        if cleanup:
            lock = conn.execute("SELECT owner,case_id,metadata_json FROM locks WHERE lock_key='board1'").fetchone()
            if (lock is None or lock["case_id"] != case_id
                    or lock["owner"] != f"{case_id}:{session_id}"
                    or json.loads(lock["metadata_json"]).get("session_id") != session_id):
                raise ApprovalError("board cleanup no longer owns the exact board session")
            if action.get("type") not in {"enter_brom", "serial_wait"}:
                raise ExecutorError(
                    "board cleanup permits only BROM entry and fresh serial wait"
                )
            approved = conn.execute(
                """SELECT 1 FROM approvals WHERE approval_type='board1_lease' AND case_id=?
                   AND session_id=? AND status IN ('approved','expired') AND consumed_at IS NULL""",
                (case_id, session_id),
            ).fetchone()
            if approved is None:
                raise ApprovalError("board cleanup has no prior Case/session lease")
        elif valid_board_lease(conn, case_id=case_id, session_id=session_id) is None:
            raise ApprovalError("no valid board1 occupancy lease")
        argv, timeout = self._argv(action)
        action_key = f"{case_id}:board1:{session_id}:{digest(action)}"
        if operation_id is not None:
            from uuid import UUID
            if not isinstance(operation_id, str) or str(UUID(operation_id)) != operation_id:
                raise ExecutorError("canonical board operation identifier required")
            action_key += ":request:" + operation_id
        if cleanup:
            # Cleanup actions are deliberately repeat-safe: entering BROM is
            # idempotent and serial_wait is read-only/fresh. A timeout after a
            # successful control pulse must not permanently strand the lease.
            # Each retry gets its own audit record and fresh evidence.
            attempt = cleanup_attempt_id or new_id("attempt")
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", attempt):
                raise ExecutorError("cleanup attempt ID is invalid")
            action_key += f":cleanup:{attempt}"
        now = iso_now()
        with transaction(conn):
            existing = conn.execute(
                "SELECT state,result_json FROM action_ledger WHERE action_key=?",
                (action_key,),
            ).fetchone()
            if existing and existing["state"] == "verified":
                value = json.loads(existing["result_json"])
                return ExecutionResult(
                    value["argv"], value["returncode"], value["stdout"], value["stderr"]
                )
            if existing is not None:
                raise ExecutorError(
                    f"board action has prior {existing['state']} state; do not repeat an uncertain side effect"
                )
            conn.execute(
                """INSERT INTO action_ledger(action_key,action_type,case_id,state,input_digest,
                       started_at,created_at,updated_at) VALUES(?, 'board',?,'started',?,?,?,?)""",
                (action_key, case_id, digest(action), now, now, now),
            )
        result = (result_runner or self.runner)(argv, None, timeout)
        state = "verified" if result.returncode == 0 else "failed"
        with transaction(conn):
            conn.execute(
                """UPDATE action_ledger SET state=?,result_json=?,finished_at=?,updated_at=? WHERE action_key=?""",
                (
                    state,
                    canonical_json(result.__dict__),
                    iso_now(),
                    iso_now(),
                    action_key,
                ),
            )
        if result.returncode != 0:
            raise ExecutorError(f"board action failed with exit {result.returncode}")
        if action["type"] in {"ram_boot", "serial_wait", "serial_exec", "enter_brom"}:
            case = conn.execute(
                "SELECT requester_id,disclosure_class FROM cases WHERE case_id=?",
                (case_id,),
            ).fetchone()
            source_id = f"src_{digest({'case': case_id, 'session': session_id, 'board': 'board1'})[:32]}"
            evidence_id = f"evd_{digest({'action_key': action_key, 'output': result.output_digest})[:32]}"
            layer = "ram_boot" if action["type"] == "ram_boot" else "device_function"
            now = iso_now()
            with transaction(conn):
                conn.execute(
                    """INSERT OR IGNORE INTO case_sources(source_id,case_id,source_type,
                           stable_external_id,title,source_version,visibility,requester_access,
                           authority,updated_at,metadata_json)
                       VALUES(?,?,'board1_session',?,'board1 verified session',?,?,?,1,?,?)""",
                    (
                        source_id,
                        case_id,
                        session_id,
                        session_id,
                        case["disclosure_class"],
                        requester_access_for_case(
                            conn,
                            case_id=case_id,
                            visibility=str(case["disclosure_class"]),
                        ),
                        now,
                        canonical_json({"board": "board1", "session_id": session_id}),
                    ),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO evidence(evidence_id,case_id,source_id,evidence_layer,
                           freshness_at,visibility,artifact_hash,claim,result,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        evidence_id,
                        case_id,
                        source_id,
                        layer,
                        now,
                        case["disclosure_class"],
                        result.output_digest,
                        f"board1 {action['type']} completed in session {session_id}",
                        canonical_json(
                            {
                                "action": action,
                                "output_digest": result.output_digest,
                                "returncode": result.returncode,
                                "verified": True,
                            }
                        ),
                        now,
                    ),
                )
        return result

    def close_session(
        self, conn: sqlite3.Connection, *, case_id: str, session_id: str, observer=None, authorize=None,
        cleanup_attempt_id: str | None = None,
    ) -> list[ExecutionResult]:
        from .board_operation_guard import hold

        with hold(self.config):
            if authorize is None and conn.execute(
                "SELECT 1 FROM broker_board_cleanup WHERE state IN ('running','unknown') LIMIT 1"
            ).fetchone():
                raise ExecutorError("unresolved broker cleanup prevents legacy retry")
            if authorize is not None:
                authorize()
            from .board_serial_observer import endpoint, observe_reset

            target = endpoint(self.config.raw["runtime"])
            if observer is None and target is not None:
                def heartbeat():
                    if authorize is not None:
                        authorize()
                    lock = conn.execute("SELECT owner,case_id,metadata_json FROM locks WHERE lock_key='board1'").fetchone()
                    approved = conn.execute(
                        "SELECT 1 FROM approvals WHERE approval_type='board1_lease' AND case_id=? AND session_id=? "
                        "AND status IN ('approved','expired') AND consumed_at IS NULL", (case_id, session_id),
                    ).fetchone()
                    if (not lock or not approved or lock["case_id"] != case_id
                            or lock["owner"] != f"{case_id}:{session_id}"
                            or json.loads(lock["metadata_json"]).get("session_id") != session_id):
                        raise ApprovalError("board cleanup no longer owns the exact board session")
                observer = lambda *, trigger: observe_reset(socket_path=target[0], daemon_uid=target[1],
                                                            trigger=trigger, heartbeat=heartbeat)
            return self._close_session_locked(conn, case_id=case_id, session_id=session_id, observer=observer,
                                              cleanup_attempt_id=cleanup_attempt_id)

    def _close_session_locked(
        self, conn: sqlite3.Connection, *, case_id: str, session_id: str, observer=None,
        cleanup_attempt_id: str | None = None,
    ) -> list[ExecutionResult]:
        # Legacy expiry/reconcile callers share this gate with broker cleanup.
        # An expired occupancy lease does not prove an in-flight device command
        # has stopped. Never reset under an unresolved broker operation.
        if conn.execute(
            "SELECT 1 FROM broker_board_actions a LEFT JOIN broker_board_results r USING(request_id) "
            "WHERE a.state IN ('queued','running','unknown') "
            "OR (a.state='cancelled' AND r.request_id IS NOT NULL) "
            "OR (a.state IN ('succeeded','failed') AND (r.request_id IS NULL OR NOT "
            "((a.state='succeeded' AND r.exit_code=0) OR "
            "(a.state='failed' AND r.exit_code BETWEEN 1 AND 254 AND r.exit_code NOT IN (124,125))))) LIMIT 1"
        ).fetchone():
            raise ExecutorError("unresolved broker board operation prevents cleanup")
        # Fresh BROM evidence requires both the control action and a new serial ROM marker.
        cleanup_attempt_id = cleanup_attempt_id or new_id("cleanup")
        results = []
        def reset():
            if results:
                raise ExecutorError("cleanup observer attempted more than one reset")
            results.append(self._execute_locked(
                conn,
                case_id=case_id,
                session_id=session_id,
                action={"type": "enter_brom"},
                cleanup=True,
                cleanup_attempt_id=cleanup_attempt_id,
            ))
        observation = None
        if observer is None:
            reset()
        else:
            observation = observer(trigger=reset)
            if len(results) != 1:
                raise ExecutorError("cleanup observer did not execute reset")
            from .board_serial_evidence import fresh_match

            fresh_match(json.dumps(observation), "usb_init : enter|usb_core_init : enter|ROM: usb download handler")
        results.append(self._execute_locked(
                conn,
                case_id=case_id,
                session_id=session_id,
                action={
                    "type": "serial_wait",
                    "regex": "usb_init : enter|usb_core_init : enter|ROM: usb download handler",
                    "timeout": 45,
                },
                cleanup=True,
                cleanup_attempt_id=cleanup_attempt_id,
                result_runner=(lambda argv, cwd, timeout: ExecutionResult(argv, 0, json.dumps(observation), "")) if observer is not None else None,
            ))
        now = iso_now()
        with transaction(conn):
            changed = conn.execute(
                """UPDATE approvals SET status='consumed',consumed_at=?,updated_at=?
                   WHERE approval_type='board1_lease' AND case_id=? AND session_id=?
                   AND status IN ('approved','expired') AND consumed_at IS NULL""",
                (now, now, case_id, session_id),
            )
            if changed.rowcount != 1:
                raise ExecutorError(
                    "board session lease could not be closed exactly once"
                )
            deleted = conn.execute(
                "DELETE FROM locks WHERE lock_key='board1' AND owner=? AND case_id=?",
                (f"{case_id}:{session_id}", case_id),
            )
            if deleted.rowcount != 1:
                raise ExecutorError("board1 lock could not be released exactly once")
        return results


def _parse_wip_command(action: dict[str, Any]) -> tuple[str, str, str]:
    command = action.get("command")
    destination = action.get("destination")
    if (
        action.get("mode") != "WIP"
        or not isinstance(command, list)
        or command[:2] != ["git", "push"]
    ):
        raise ExecutorError("only exact git push actions in WIP mode are allowed")
    if not all(isinstance(part, str) and part for part in command):
        raise ExecutorError("push command must be a non-empty argv list")
    if any(part in {"--force", "-f", "--mirror", "--delete"} for part in command):
        raise ExecutorError("force, mirror, and delete pushes are forbidden")

    positional = command[:2]
    has_wip_option = False
    index = 2
    while index < len(command):
        part = command[index]
        if part == "-o":
            if index + 1 >= len(command) or command[index + 1] != "wip":
                raise ExecutorError("only the Gerrit wip push option is allowed")
            has_wip_option = True
            index += 2
            continue
        if part == "--push-option=wip":
            has_wip_option = True
            index += 1
            continue
        if part.startswith("-"):
            raise ExecutorError(f"unsupported git push option: {part}")
        positional.append(part)
        index += 1
    if len(positional) != 4:
        raise ExecutorError(
            "push command must contain exactly one remote and one refspec"
        )
    remote, refspec = positional[2:]
    if refspec.count(":") != 1:
        raise ExecutorError(
            "push command must use an explicit source:destination refspec"
        )
    source, command_destination = refspec.split(":", 1)
    if (
        not source
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", source)
        or ".." in source
        or command_destination != destination
    ):
        raise ExecutorError("push refspec does not match the approved destination")
    if not isinstance(destination, str) or not re.fullmatch(
        r"refs/for/[A-Za-z0-9._/-]+(?:%[A-Za-z0-9._,=+-]+)?", destination
    ):
        raise ExecutorError("destination is not a valid Gerrit refs/for target")
    push_options = (
        destination.partition("%")[2].split(",") if "%" in destination else []
    )
    has_wip = "wip" in push_options
    if any(option != "wip" for option in push_options) or len(push_options) > 1:
        raise ExecutorError("only the Gerrit wip destination option is allowed")
    if not (has_wip or has_wip_option):
        raise ExecutorError("push command does not force Gerrit WIP")
    return remote, source, destination


def _validate_wip_request(action: dict[str, Any], config: Config | None = None) -> None:
    _parse_wip_command(action)
    commits = action.get("commits")
    if (
        not isinstance(commits, list)
        or not commits
        or not all(
            isinstance(commit, str) and re.fullmatch(r"[0-9a-fA-F]{40}", commit)
            for commit in commits
        )
    ):
        raise ExecutorError("approved commits must be exact 40-character object IDs")
    if len(commits) > 256 or len({item.lower() for item in commits}) != len(commits):
        raise ExecutorError("approved commits must be unique and limited to 256")
    worktree = action.get("worktree")
    if worktree is not None:
        case_id = action.get("case_id")
        if not isinstance(case_id, str) or not re.fullmatch(r"K3-\d{8}-\d{4}", case_id):
            raise ExecutorError("WIP worktree requires a valid Case ID")
        worktree_root = (
            PurePosixPath(config.runtime("remote_worktree_root"))
            if config is not None
            else PurePosixPath("/data/home2/operator/WorkSpace/k3-ai-worktrees")
        )
        root = worktree_root / case_id
        path = PurePosixPath(str(worktree))
        if (
            not str(worktree).startswith("/")
            or ".." in path.parts
            or root not in path.parents
            or any(
                not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in path.parts[1:]
            )
        ):
            raise ExecutorError("WIP worktree is outside the exact Case root")


def validate_wip_action(action: dict[str, Any], config: Config | None = None) -> None:
    """Validate a bound approval; legacy requests need a freshly observed preview."""
    _validate_wip_request(action, config)
    binding = action.get("binding")
    fields = {
        "version",
        "remote_url",
        "project",
        "destination_branch",
        "base_sha",
        "tip_sha",
    }
    if (
        not isinstance(binding, dict)
        or set(binding) != fields
        or type(binding["version"]) is not int
        or binding["version"] != 1
    ):
        raise ExecutorError("WIP approval lacks exact binding; create a fresh preview")
    for field in ("base_sha", "tip_sha"):
        if not isinstance(binding[field], str) or not re.fullmatch(
            r"[0-9a-f]{40}", binding[field]
        ):
            raise ExecutorError(f"WIP binding {field} must be a full lowercase SHA")
    if (
        binding["tip_sha"] != action["commits"][-1]
        or binding["base_sha"] in action["commits"]
    ):
        raise ExecutorError("WIP binding does not match its pending commit set")
    project, _, _ = _parse_gerrit_remote(binding["remote_url"])
    if (
        project != binding["project"]
        or _gerrit_branch(action["destination"]) != binding["destination_branch"]
    ):
        raise ExecutorError("WIP binding project/destination mismatch")
    if config is not None:
        repo = config.raw["repositories"].get(action.get("repo"))
        if (
            not isinstance(repo, dict)
            or _parse_wip_command(action)[0] != repo["remote"]
        ):
            raise ExecutorError(
                "push remote does not match the configured repository remote"
            )


def _wip_git(config: Config, runner: Runner, path: str, args: list[str]) -> str:
    command = " ".join(shlex.quote(part) for part in ["git", "-C", path, *args])
    result = runner(
        command_argv(config, command),
        None,
        60,
    )
    if result.returncode != 0:
        raise ExecutorError(
            f"WIP preview/preflight failed at git {args[0]}; create a fresh preview"
        )
    return result.stdout.strip()


def bind_wip_action(
    config: Config, action: dict[str, Any], *, runner: Runner = run_process
) -> dict[str, Any]:
    """Observe the exact target and complete linear range using read-only Git calls.

    This produces a preview, never approval. No fetch, checkout or push is used.
    The destination base must already exist locally, otherwise the caller must
    refresh its isolated checkout and request another preview.
    """
    _validate_wip_request(action, config)
    remote, source, destination = _parse_wip_command(action)
    repo = config.raw["repositories"].get(action.get("repo"))
    if not isinstance(repo, dict) or remote != repo["remote"]:
        raise ExecutorError(
            "push remote does not match the configured repository remote"
        )
    path = str(action.get("worktree") or repo["path"])
    if _wip_git(config, runner, path, ["rev-parse", "--show-toplevel"]) != path:
        raise ExecutorError("WIP checkout is not its exact approved top-level")
    remote_url = _wip_git(
        config, runner, path, ["remote", "get-url", "--push", "--all", remote]
    )
    project, _, _ = _parse_gerrit_remote(remote_url)
    branch = _gerrit_branch(destination)
    target_ref = f"refs/heads/{branch}"
    advertised = _wip_git(
        config,
        runner,
        path,
        ["ls-remote", "--refs", "--exit-code", remote_url, target_ref],
    )
    advertised_fields = advertised.split()
    if (
        len(advertised_fields) != 2
        or advertised_fields[1] != target_ref
        or not re.fullmatch(r"[0-9a-f]{40}", advertised_fields[0])
    ):
        raise ExecutorError("WIP destination must advertise exactly one base SHA")
    base = advertised_fields[0]
    tip = _wip_git(
        config,
        runner,
        path,
        ["rev-parse", "--verify", "--end-of-options", source + "^{commit}"],
    )
    if tip != action["commits"][-1]:
        raise ExecutorError("push source does not resolve to the approved tip commit")
    history = _wip_git(
        config,
        runner,
        path,
        [
            "rev-list",
            "--reverse",
            "--topo-order",
            "--parents",
            "--max-count=257",
            f"{base}..{tip}",
        ],
    )
    commits: list[str] = []
    parent = base
    for line in history.splitlines():
        fields = line.split()
        if (
            len(fields) != 2
            or fields[1] != parent
            or any(not re.fullmatch(r"[0-9a-f]{40}", value) for value in fields)
        ):
            raise ExecutorError(
                "WIP pending history must be a complete linear range; merge commits are unsupported"
            )
        commits.append(fields[0])
        parent = fields[0]
    if commits != action["commits"]:
        raise ExecutorError(
            "WIP pending commit set is incomplete, extra or out of order"
        )
    bound = {
        **action,
        "binding": {
            "version": 1,
            "remote_url": remote_url,
            "project": project,
            "destination_branch": branch,
            "base_sha": base,
            "tip_sha": tip,
        },
    }
    validate_wip_action(bound, config)
    return bound


def _parse_gerrit_remote(remote_url: str) -> tuple[str, str, int]:
    if (
        not isinstance(remote_url, str)
        or not remote_url
        or any(character.isspace() or ord(character) < 32 for character in remote_url)
    ):
        raise ExecutorError("configured Gerrit remote must be one exact SSH URL")
    value = remote_url.strip()
    parsed = urlparse(value)
    if parsed.scheme:
        if (
            parsed.scheme != "ssh"
            or not parsed.hostname
            or not parsed.username
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ExecutorError(
                "configured Gerrit remote is not an authenticated SSH URL"
            )
        project = parsed.path.lstrip("/")
        endpoint = f"{parsed.username}@{parsed.hostname}"
        try:
            port = parsed.port or 29418
        except ValueError as exc:
            raise ExecutorError("configured Gerrit remote port is invalid") from exc
    elif ":" in value:
        endpoint, project = value.split(":", 1)
        project = project.lstrip("/")
        if "@" not in endpoint:
            raise ExecutorError("configured Gerrit remote has no SSH user")
        port = 29418
    else:
        raise ExecutorError("configured Gerrit remote is not an SSH URL")
    project = project.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", project) or any(
        part in {"", ".", ".."} for part in project.split("/")
    ):
        raise ExecutorError("cannot derive Gerrit project from repository remote")
    return project, endpoint, port


def _gerrit_branch(destination: str) -> str:
    branch = destination.removeprefix("refs/for/").partition("%")[0]
    parts = branch.split("/")
    if (
        not branch
        or branch.startswith("/")
        or branch.endswith(("/", "."))
        or ".." in branch
        or "@{" in branch
        or any(part in {"", ".", ".."} or part.endswith(".lock") for part in parts)
    ):
        raise ExecutorError("cannot derive Gerrit branch from destination")
    return branch


def verify_gerrit_wip(
    config: Config,
    action: dict[str, Any],
    _push_result: ExecutionResult,
    *,
    runner: Runner = run_process,
    attempts: int = 3,
) -> dict[str, Any]:
    """Read Gerrit back and prove every approved commit is the current WIP revision."""
    validate_wip_action(action, config)
    repo = config.raw["repositories"].get(action.get("repo"))
    if not isinstance(repo, dict):
        raise ExecutorError("repository mapping is unavailable")
    # Verification uses the approved endpoint even if a local remote alias moves.
    project, endpoint, port = _parse_gerrit_remote(action["binding"]["remote_url"])
    branch = action["binding"]["destination_branch"]
    verified: list[dict[str, Any]] = []
    errors: list[str] = []
    for commit in action["commits"]:
        match: dict[str, Any] | None = None
        last_error: str | None = None
        for attempt in range(max(1, attempts)):
            query_argv = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "-p",
                str(port),
                endpoint,
                "gerrit",
                "query",
                "--format=JSON",
                "--current-patch-set",
                f"commit:{commit}",
                f"project:{project}",
                f"branch:{branch}",
                "limit:20",
            ]
            query_result = runner(
                command_argv(config, " ".join(shlex.quote(part) for part in query_argv)),
                None,
                60,
            )
            if query_result.returncode != 0:
                last_error = (
                    f"Gerrit query failed for {commit}: {query_result.stderr.strip()}"
                )
            else:
                try:
                    payload = [
                        json.loads(line)
                        for line in query_result.stdout.splitlines()
                        if line.strip()
                    ]
                except json.JSONDecodeError as exc:
                    last_error = (
                        f"Gerrit query returned invalid JSON for {commit}: {exc}"
                    )
                else:
                    candidates = [
                        change
                        for change in payload
                        if change.get("type") != "stats"
                        if change.get("project") == project
                        and change.get("branch") == branch
                        and change.get("currentPatchSet", {}).get("revision") == commit
                    ]
                    if len(candidates) == 1:
                        match = candidates[0]
                        break
                    last_error = (
                        f"expected one current Gerrit change for {project}/{branch}@{commit}, "
                        f"found {len(candidates)}"
                    )
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
        if match is None:
            errors.append(last_error or f"no Gerrit result for {commit}")
            continue
        revision = match.get("currentPatchSet", {})
        item = {
            "change": str(match.get("number")),
            "revision": commit,
            "patch_set": revision.get("number"),
            "wip": match.get("wip") is True,
            "project": project,
            "branch": branch,
        }
        verified.append(item)
        if item["wip"] is not True:
            errors.append(f"change {item['change']} is not Work in Progress")
        if not item["patch_set"]:
            errors.append(
                f"change {item['change']} has no Patch Set for revision {commit}"
            )
    all_verified = len(verified) == len(action["commits"]) and all(
        item["wip"] is True and item["patch_set"] for item in verified
    )
    tail = verified[-1] if verified else {}
    return {
        "change": tail.get("change"),
        "revision": tail.get("revision"),
        "patch_set": tail.get("patch_set"),
        "wip": all_verified,
        "changes": verified,
        "errors": errors,
        "project": project,
        "branch": branch,
    }


def execute_wip_push(
    conn: sqlite3.Connection,
    config: Config,
    *,
    case_id: str,
    action: dict[str, Any],
    runner: Runner = run_process,
    verifier: Callable[[dict[str, Any], ExecutionResult], dict[str, Any]] | None = None,
    job_id: str | None = None,
    lease_owner: str | None = None,
    attempt_no: int | None = None,
) -> dict[str, Any]:
    if not config.feature("wip_push"):
        raise ExecutorError("WIP push feature is disabled")
    from .runtime_control import capability_allowed

    if not capability_allowed(conn, config, "wip_push"):
        raise ExecutorError("WIP push is disabled by the global runtime mode")
    validate_wip_action(action, config)
    if action.get("case_id") != case_id:
        raise ExecutorError("push action Case ID does not match the execution Case")
    approval = valid_push_approval(conn, case_id=case_id, action=action)
    if approval is None:
        raise ApprovalError("no exact unconsumed WIP push approval")
    repo_name = action.get("repo")
    repo = config.raw["repositories"].get(repo_name)
    if not isinstance(repo, dict) or not isinstance(repo.get("path"), str):
        raise ExecutorError("repository mapping is unavailable")
    configured_path = PurePosixPath(repo["path"])
    source_root = PurePosixPath(config.runtime("remote_source_root"))
    if source_root not in configured_path.parents:
        raise ExecutorError("repository path is outside remote_source_root")
    repo_path = str(action.get("worktree") or repo["path"])

    def require_authority() -> None:
        if not capability_allowed(conn, config, "wip_push"):
            raise ExecutorError("WIP push was paused before dispatch")
        case = conn.execute(
            "SELECT state FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if case is None or case["state"] != "waiting_push":
            raise ExecutorError("Case no longer permits WIP push")
        if job_id is not None:
            job = conn.execute(
                """SELECT 1 FROM jobs WHERE job_id=? AND case_id=? AND job_type='push'
                   AND state='running' AND lease_owner=? AND attempt_no=? AND lease_expires_at>?""",
                (job_id, case_id, lease_owner, attempt_no, iso_now()),
            ).fetchone()
            if job is None:
                raise ExecutorError("WIP job lost its active claim before dispatch")

    require_authority()
    observed = bind_wip_action(config, action, runner=runner)
    if observed["binding"] != action["binding"]:
        raise ExecutorError("WIP remote/base binding changed; create a fresh preview")
    binding = action["binding"]
    # Both values are immutable parts of the owner-approved action. Mutable
    # source/remote aliases are deliberately absent from the network command.
    network_command = [
        "git",
        "-C",
        repo_path,
        "push",
        "-o",
        "wip",
        binding["remote_url"],
        f"{binding['tip_sha']}:refs/for/{binding['destination_branch']}",
    ]
    command = " ".join(shlex.quote(part) for part in network_command)
    argv = command_argv(config, command)
    action_key = f"{case_id}:push:{approval['action_digest']}"
    now = iso_now()
    with transaction(conn):
        require_authority()
        if valid_push_approval(conn, case_id=case_id, action=action) is None:
            raise ApprovalError("WIP approval changed during preflight")
        conn.execute(
            """INSERT INTO action_ledger(action_key,action_type,case_id,state,input_digest,
                   started_at,created_at,updated_at) VALUES(?,'push',?,'started',?,?,?,?)""",
            (action_key, case_id, approval["action_digest"], now, now, now),
        )
        changed = conn.execute(
            """UPDATE approvals SET status='consumed',consumed_at=?,updated_at=?
               WHERE approval_id=? AND status='approved' AND consumed_at IS NULL
                 AND action_digest=? AND expires_at>?""",
            (now, now, approval["approval_id"], approval["action_digest"], now),
        ).rowcount
        if changed != 1:
            raise ApprovalError("WIP approval was already consumed")
    # The dispatch transaction wins before the side effect. Revocation after
    # this point cannot prove the push never started; receipts remain historical.
    try:
        result = runner(argv, None, 300)
    except Exception as exc:
        conn.execute(
            "UPDATE action_ledger SET state='uncertain',result_json=?,finished_at=?,updated_at=? WHERE action_key=?",
            (
                canonical_json(
                    {"error_class": type(exc).__name__, "no_automatic_retry": True}
                ),
                iso_now(),
                iso_now(),
                action_key,
            ),
        )
        raise ExecutorError(
            "WIP push outcome is uncertain; no automatic retry"
        ) from exc
    if result.returncode != 0:
        with transaction(conn):
            conn.execute(
                "UPDATE action_ledger SET state='uncertain',result_json=?,finished_at=?,updated_at=? WHERE action_key=?",
                (canonical_json(result.__dict__), iso_now(), iso_now(), action_key),
            )
        raise ExecutorError(f"WIP push failed with exit {result.returncode}")
    try:
        verification = (
            verifier(action, result)
            if verifier is not None
            else verify_gerrit_wip(config, action, result, runner=runner)
        )
    except Exception as exc:  # noqa: BLE001 - an already-completed push must be recorded as uncertain
        verification = {
            "change": None,
            "revision": None,
            "patch_set": None,
            "wip": False,
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    if not (
        verification.get("wip") is True
        and verification.get("revision") == binding["tip_sha"]
        and verification.get("patch_set")
    ):
        state = "uncertain"
    else:
        state = "verified"
    with transaction(conn):
        conn.execute(
            "UPDATE action_ledger SET state=?,result_json=?,remote_id=?,finished_at=?,updated_at=? WHERE action_key=?",
            (
                state,
                canonical_json(
                    {"execution": result.__dict__, "verification": verification}
                ),
                verification.get("change"),
                iso_now(),
                iso_now(),
                action_key,
            ),
        )
    if state != "verified":
        raise ExecutorError(
            "push completed but Gerrit WIP/revision/Patch Set verification is incomplete"
        )
    return verification
