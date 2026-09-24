from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from .approvals import (
    decide_approval,
    expiry_after,
    normalized_board_action,
    normalized_push_action,
    request_approval,
    valid_board_lease,
    valid_push_approval,
)
from .config import Config, load_config
from .db import connect, integrity, migrate
from .decision import apply_decision
from .execution_transport import command_argv
from .store import (
    EXECUTABLE_CASE_STATES,
    create_case,
    enqueue_outbox,
    get_case,
    ingest_event,
    merge_case,
    transition_case,
)
from .timeutil import iso_now

DEFAULT_CONFIG = Path("~/.hermes/k3-support/config.yaml").expanduser()


def _json_arg(value: str) -> Any:
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    return json.loads(value)


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _config(args: argparse.Namespace) -> Config:
    return load_config(args.config)


def _conn(args: argparse.Namespace) -> sqlite3.Connection:
    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    return conn


def _readonly_conn(args: argparse.Namespace) -> sqlite3.Connection:
    return _existing_schema_conn(args, writable=False)


def _existing_schema_conn(args: argparse.Namespace, *, writable: bool) -> sqlite3.Connection:
    from .db import DatabaseError, migration_files

    database = _config(args).database_path.resolve()
    if not database.is_file():
        raise FileNotFoundError(
            "existing workflow database is required; these commands do not initialize it"
        )
    conn = sqlite3.connect(
        database.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        present = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        }
        if present != {version for version, _, _ in migration_files()}:
            raise DatabaseError(
                "database schema differs; migrate explicitly before using this command"
            )
        return conn
    except BaseException:
        conn.close()
        raise


def _control(args: argparse.Namespace, cfg: Config) -> tuple[str, str]:
    user = args.control_user_id or os.environ.get("K3_SUPPORT_CONTROL_USER_ID")
    chat = args.control_chat_id or os.environ.get("K3_SUPPORT_CONTROL_CHAT_ID")
    if not user or not chat:
        raise ValueError("stable control user and chat IDs are required")
    from .approvals import verify_control_identity

    verify_control_identity(cfg, user, chat)
    return user, chat


def cmd_init_db(args: argparse.Namespace) -> None:
    cfg = _config(args)
    cfg.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = connect(cfg.database_path)
    applied = migrate(conn)
    from .runtime_control import ensure_global_state

    runtime = ensure_global_state(
        conn,
        actor_id="deployment",
        source="init_db",
        external_id="init-db:global-controller",
    )
    _emit(
        {
            "database": str(cfg.database_path),
            "applied_migrations": applied,
            "integrity": integrity(conn),
            "mode": cfg.mode,
            "runtime_mode": runtime["mode"],
        }
    )


def cmd_config_migration_preview(args: argparse.Namespace) -> None:
    from .config_migration import preview_config_migration

    report = preview_config_migration(
        args.config,
        legacy_runtime_bin=Path(args.legacy_runtime_bin)
        if args.legacy_runtime_bin
        else None,
        legacy_home=Path(args.legacy_home) if args.legacy_home else None,
    )
    if args.output:
        target = Path(args.output).expanduser().absolute()
        # New private copy only: never replace the source or an existing output.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(report["proposed_yaml"])
            stream.flush()
            os.fsync(stream.fileno())
        report["output_path"] = str(target)
        report["output_mode"] = "0600"
    _emit(report)


def cmd_ingest(args: argparse.Namespace) -> None:
    conn = _conn(args)
    event_pk, created = ingest_event(
        conn,
        source=args.source,
        identity=args.identity,
        external_id=args.external_id,
        payload=_json_arg(args.payload),
        occurred_at=args.occurred_at,
        sender_id=args.sender_id,
        chat_id=args.chat_id,
        thread_id=args.thread_id,
    )
    _emit({"event_pk": event_pk, "created": created})


def cmd_create_case(args: argparse.Namespace) -> None:
    conn = _conn(args)
    case_id, created = create_case(
        conn,
        title=args.title,
        case_type=args.type,
        severity=args.severity,
        confidence=args.confidence,
        requester_id=args.requester_id,
        requester_chat_id=args.requester_chat_id,
        disclosure_class=args.disclosure_class,
        source_event_pk=args.source_event_pk,
        idempotency_key=args.idempotency_key,
    )
    _emit({"case_id": case_id, "created": created})


def cmd_status(args: argparse.Namespace) -> None:
    conn = _conn(args)
    if args.case_id:
        _emit(get_case(conn, args.case_id))
        return
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT case_id,title,severity,state,owner,version,updated_at,next_action FROM cases ORDER BY updated_epoch DESC LIMIT ?",
            (args.limit,),
        )
    ]
    _emit({"cases": rows})


def _controlled_transition(args: argparse.Namespace, after: str) -> None:
    cfg = _config(args)
    user, _ = _control(args, cfg)
    conn = connect(cfg.database_path)
    migrate(conn)
    version = transition_case(
        conn,
        case_id=args.case_id,
        after=after,
        actor_type="operator",
        actor_id=user,
        reason=args.reason,
        expected_version=args.expected_version,
        idempotency_key=args.command_id,
    )
    _emit({"case_id": args.case_id, "state": after, "version": version})


def cmd_transition(args: argparse.Namespace) -> None:
    conn = _conn(args)
    version = transition_case(
        conn,
        case_id=args.case_id,
        after=args.after,
        actor_type=args.actor_type,
        actor_id=args.actor_id,
        reason=args.reason,
        expected_version=args.expected_version,
        idempotency_key=args.idempotency_key,
    )
    _emit({"case_id": args.case_id, "state": args.after, "version": version})


def cmd_merge_case(args: argparse.Namespace) -> None:
    conn = _conn(args)
    version = merge_case(
        conn,
        case_id=args.case_id,
        canonical_case_id=args.canonical_case_id,
        actor_type=args.actor_type,
        actor_id=args.actor_id,
        reason=args.reason,
        expected_version=args.expected_version,
        idempotency_key=args.idempotency_key,
    )
    _emit(
        {
            "case_id": args.case_id,
            "canonical_case_id": args.canonical_case_id,
            "state": "cancelled",
            "version": version,
        }
    )


def cmd_pause(args: argparse.Namespace) -> None:
    _controlled_transition(args, "paused")


def cmd_takeover(args: argparse.Namespace) -> None:
    _controlled_transition(args, "takeover")


def cmd_cancel(args: argparse.Namespace) -> None:
    _controlled_transition(args, "cancelled")


def cmd_resume(args: argparse.Namespace) -> None:
    cfg = _config(args)
    user, _ = _control(args, cfg)
    conn = connect(cfg.database_path)
    migrate(conn)
    case = get_case(conn, args.case_id)
    if case["state"] != "paused":
        raise ValueError("resume requires a paused case")
    pause_event = next(
        (e for e in case["events"] if e["after_state"] == "paused"), None
    )
    if pause_event is None or pause_event["before_state"] is None:
        raise ValueError("cannot determine pre-pause state")
    version = transition_case(
        conn,
        case_id=args.case_id,
        after=pause_event["before_state"],
        actor_type="operator",
        actor_id=user,
        reason=args.reason,
        expected_version=args.expected_version,
        idempotency_key=args.command_id,
    )
    _emit(
        {
            "case_id": args.case_id,
            "state": pause_event["before_state"],
            "version": version,
        }
    )


def cmd_request_board(args: argparse.Namespace) -> None:
    conn = _conn(args)
    action = normalized_board_action(
        args.case_id, args.session_id, args.estimated_minutes
    )
    approval_id, action_digest, created = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=args.case_id,
        session_id=args.session_id,
        action=action,
        expires_at=expiry_after(args.request_valid_minutes),
    )
    _emit(
        {
            "approval_id": approval_id,
            "digest": action_digest,
            "created": created,
            "action": action,
        }
    )


def cmd_request_push(args: argparse.Namespace) -> None:
    from .executors import bind_wip_action

    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    action = normalized_push_action(
        case_id=args.case_id,
        repo=args.repo,
        destination=args.destination,
        commits=args.commit,
        command=args.command,
        worktree=args.worktree,
    )
    action = bind_wip_action(cfg, action)
    approval_id, action_digest, created = request_approval(
        conn,
        approval_type="wip_push",
        case_id=args.case_id,
        action=action,
        expires_at=expiry_after(args.valid_minutes),
    )
    _emit(
        {
            "approval_id": approval_id,
            "digest": action_digest,
            "created": created,
            "action": action,
        }
    )


def cmd_decide_approval(args: argparse.Namespace) -> None:
    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    result = decide_approval(
        conn,
        cfg,
        approval_id=args.approval_id,
        approve=args.decision == "approve",
        approver_user_id=args.control_user_id,
        approver_chat_id=args.control_chat_id,
        message_id=args.message_id,
        decision_text=args.decision_text,
        expected_digest=args.digest,
    )
    _emit(result)


def cmd_gate(args: argparse.Namespace) -> None:
    conn = _conn(args)
    if args.gate == "board":
        value = valid_board_lease(
            conn, case_id=args.case_id, session_id=args.session_id
        )
    else:
        value = valid_push_approval(
            conn, case_id=args.case_id, action=_json_arg(args.action)
        )
    _emit({"allowed": value is not None, "approval": value})


def _execution_payload(result: Any) -> dict[str, Any]:
    return {
        "argv": result.argv,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "output_digest": result.output_digest,
    }


def cmd_delegate_codex(args: argparse.Namespace) -> None:
    from .executors import create_codex_job

    brief = Path(args.brief).read_text(encoding="utf-8")
    job_id, created = create_codex_job(
        _conn(args), _config(args), case_id=args.case_id, brief=brief, repo=args.repo
    )
    _emit({"job_id": job_id, "created": created})


def cmd_run_codex_job(args: argparse.Namespace) -> None:
    from .executors import run_codex_job

    result = run_codex_job(_conn(args), _config(args), job_id=args.job_id)
    _emit(_execution_payload(result))


def cmd_delegate_code(args: argparse.Namespace) -> None:
    from .broker_execution_contract import load_at
    from .executors import create_codex_job

    directory = Path(args.contract_directory)
    if not directory.is_absolute() or ".." in directory.parts:
        raise ValueError("absolute deployment contract directory required")
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        contract = load_at(fd, control_uid=os.geteuid(), worker_uid=args.worker_uid)
    finally:
        os.close(fd)
    brief = Path(args.brief).read_text(encoding="utf-8")
    conn = _existing_schema_conn(args, writable=True)
    try:
        job_id, created = create_codex_job(conn, _config(args), case_id=args.case_id, brief=brief,
                                           repo=args.repo, execution_contract=contract)
    finally:
        conn.close()
    _emit({"job_id": job_id, "created": created, "execution": contract.selection(),
           "model": contract.model, "reasoning": contract.reasoning})


def cmd_review_codex_job(args: argparse.Namespace) -> None:
    from .review import prepare_codex_review

    _emit(prepare_codex_review(_conn(args), _config(args), job_id=args.job_id))


def cmd_run_hermes_review(args: argparse.Namespace) -> None:
    from .review import run_hermes_review

    cfg = _config(args)
    if cfg.mode != "active" or not cfg.feature("codex"):
        raise ValueError(
            "Hermes review execution requires Active mode and the Codex feature"
        )
    _emit(run_hermes_review(_conn(args), cfg, job_id=args.job_id))


def cmd_board_action(args: argparse.Namespace) -> None:
    from .executors import BoardExecutor

    result = BoardExecutor(_config(args)).execute(
        _conn(args),
        case_id=args.case_id,
        session_id=args.session_id,
        action=_json_arg(args.action),
    )
    _emit(_execution_payload(result))


def cmd_close_board_session(args: argparse.Namespace) -> None:
    from .executors import BoardExecutor

    results = BoardExecutor(_config(args)).close_session(
        _conn(args), case_id=args.case_id, session_id=args.session_id
    )
    _emit(
        {
            "case_id": args.case_id,
            "session_id": args.session_id,
            "results": [_execution_payload(item) for item in results],
        }
    )


def cmd_execute_wip_push(args: argparse.Namespace) -> None:
    from .executors import execute_wip_push

    action = _json_arg(args.action)
    _emit(
        execute_wip_push(
            _conn(args), _config(args), case_id=args.case_id, action=action
        )
    )


def cmd_apply_decision(args: argparse.Namespace) -> None:
    _emit(apply_decision(_conn(args), _json_arg(args.decision), config=_config(args)))


def cmd_enqueue(args: argparse.Namespace) -> None:
    conn = _conn(args)
    from .db import transaction

    if args.channel == "feishu_im" and args.action_type in {"reply", "ack", "clarify"}:
        raise ValueError(
            "public Feishu communication must use the Case decision path so Turn fencing is mandatory"
        )
    with transaction(conn):
        outbox_id, created = enqueue_outbox(
            conn,
            channel=args.channel,
            action_type=args.action_type,
            destination=args.destination,
            payload=_json_arg(args.payload),
            idempotency_key=args.idempotency_key,
            case_id=args.case_id,
        )
    _emit({"outbox_id": outbox_id, "created": created})


def cmd_reconcile(args: argparse.Namespace) -> None:
    from .operations import reconcile

    _emit(reconcile(_conn(args), config=_config(args)))


def cmd_health(args: argparse.Namespace) -> None:
    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    from .runtime_control import current_global_state
    from .watchdog import actionable_permanent_outbox_ids

    executable_placeholders = ",".join("?" for _ in EXECUTABLE_CASE_STATES)
    queued_ready = conn.execute(
        f"""SELECT count(*) FROM jobs j LEFT JOIN cases c USING(case_id)
              WHERE j.state='queued' AND j.available_at<=?
                AND (j.job_type='base_sync' OR c.state IN ({executable_placeholders}))""",
        (iso_now(), *EXECUTABLE_CASE_STATES),
    ).fetchone()[0]
    queued_total = conn.execute(
        "SELECT count(*) FROM jobs WHERE state='queued'"
    ).fetchone()[0]
    permanent_total = conn.execute(
        "SELECT count(*) FROM outbox WHERE state='permanent_failure'"
    ).fetchone()[0]
    knowledge = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            "SELECT status,count(*) AS count FROM knowledge_entries GROUP BY status"
        )
    }
    counts = {
        "inbox_new": conn.execute(
            "SELECT count(*) FROM inbound_events WHERE status='new'"
        ).fetchone()[0],
        "outbox_pending": conn.execute(
            "SELECT count(*) FROM outbox WHERE state IN ('pending','retry')"
        ).fetchone()[0],
        "jobs_running": conn.execute(
            "SELECT count(*) FROM jobs WHERE state='running'"
        ).fetchone()[0],
        "jobs_queued": queued_total,
        "jobs_ready": queued_ready,
        "jobs_held": queued_total - queued_ready,
        "outbox_permanent_failures": permanent_total,
        "outbox_actionable_failures": len(actionable_permanent_outbox_ids(conn, cfg)),
        "dead_letters": conn.execute(
            "SELECT count(*) FROM inbound_events WHERE status='dead_letter'"
        ).fetchone()[0],
    }
    _emit(
        {
            "mode": cfg.mode,
            "runtime_mode": current_global_state(conn, cfg)["mode"],
            "features": {name: cfg.feature(name) for name in cfg.raw["features"]},
            "base_features": cfg.raw["features"],
            "knowledge": {
                "approved": knowledge.get("approved", 0),
                "candidate": knowledge.get("candidate", 0),
                "stale": knowledge.get("stale", 0),
                "auto_faq_ready": not cfg.feature("auto_faq")
                or knowledge.get("approved", 0) > 0,
            },
            "database": integrity(conn),
            "queues": counts,
        }
    )


def cmd_readiness_report(args: argparse.Namespace) -> None:
    from .readiness import readiness_report

    cfg = _config(args)
    conn = _readonly_conn(args)
    try:
        # This read-only CLI makes no live query: do not infer an actual runtime
        # descriptor from configuration, historical routes or the signed artifact.
        _emit(readiness_report(conn, cfg))
    finally:
        conn.close()


def cmd_mail_action_status(args: argparse.Namespace) -> None:
    from .mail_actions import view

    conn = _readonly_conn(args)
    try:
        _emit(view(conn, args.message_id))
    finally:
        conn.close()


def cmd_mail_action(args: argparse.Namespace) -> None:
    from .mail_actions import apply

    user, _ = _control(args, _config(args))
    conn = _conn(args)
    try:
        _emit(apply(conn, message_id=args.message_id, action=args.action,
                    expected_revision=args.expected_revision, content_digest=args.content_digest,
                    request_id=args.request_id, actor_id=user, minutes=args.minutes,
                    case_id=args.case_id))
    finally:
        conn.close()


def cmd_notification_status(args: argparse.Namespace) -> None:
    from .notification_snooze import status

    conn = _readonly_conn(args)
    try:
        _emit(status(conn,config=_config(args)))
    finally:
        conn.close()


def cmd_notification_snooze(args: argparse.Namespace) -> None:
    from .notification_snooze import set_snooze

    cfg = _config(args)
    user, _ = _control(args, cfg)
    conn = _conn(args)
    try:
        _emit(set_snooze(conn, minutes=args.minutes, expected_revision=args.expected_revision,
            request_id=args.request_id, actor_id=user,
            night_enabled=None if args.night is None else args.night == "on"))
    finally:
        conn.close()


def cmd_knowledge_gap_report(args: argparse.Namespace) -> None:
    from .knowledge_gaps import knowledge_gap_report

    conn = _readonly_conn(args)
    try:
        _emit(
            knowledge_gap_report(
                conn,
                since=args.since,
                until=args.until,
                page=args.page,
                page_size=args.page_size,
                min_repeat=args.min_repeat,
                expected_digest=args.expected_digest,
            )
        )
    finally:
        conn.close()


def cmd_workbench(args: argparse.Namespace) -> None:
    from .workbench import workbench_page

    cfg = _config(args)
    if args.page != 1:
        raise ValueError(
            "numbered workbench pages are unsupported; use --cursor from the previous result"
        )
    conn = _readonly_conn(args)
    try:
        result = workbench_page(
            conn,
            cfg,
            limit=args.limit,
            cursor=args.cursor,
            view=args.view,
            render_buttons=False,
        )
        snapshot = result["snapshot"]
        _emit(
            {**snapshot, "text": result["preview"]["text"]} if args.text else snapshot
        )
    finally:
        conn.close()


def cmd_mail_preflight(args: argparse.Namespace) -> None:
    from .lark import mail_preflight

    cfg = _config(args)
    _emit(mail_preflight(executable=cfg.runtime("lark_cli_command")))


def cmd_office_doctor(args: argparse.Namespace) -> None:
    from .lark import run_json, run_mail_json
    from .office_preflight import office_doctor

    cfg = _config(args)
    _emit(
        office_doctor(
            cfg,
            runner=partial(run_json, executable=cfg.runtime("lark_cli_command")),
            mail_runner=partial(
                run_mail_json, executable=cfg.runtime("lark_cli_command")
            ),
        )
    )


def cmd_scope_candidates(args: argparse.Namespace) -> None:
    from .lark import run_json
    from .office_preflight import discover_group_candidates

    cfg = _config(args)
    observed = datetime.fromisoformat(args.now) if args.now else None
    if observed is not None and observed.tzinfo is None:
        raise ValueError("--now must include a timezone offset")
    _emit(
        discover_group_candidates(
            cfg,
            days=args.days,
            now=observed,
            runner=partial(run_json, executable=cfg.runtime("lark_cli_command")),
        )
    )


def cmd_requester_profile_show(args: argparse.Namespace) -> None:
    from .routing import get_requester_profile

    _emit(get_requester_profile(_conn(args), args.requester_id))


def cmd_requester_profile_set(args: argparse.Namespace) -> None:
    from .routing import set_requester_profile

    cfg = _config(args)
    _control(args, cfg)
    conn = connect(cfg.database_path)
    migrate(conn)
    _emit(
        set_requester_profile(
            conn,
            requester_id=args.requester_id,
            relationship=args.relationship,
            function_role=args.function_role,
            source="operator",
            relationship_confidence=1.0,
            function_confidence=1.0,
            display_name=args.display_name,
            department=args.department,
            job_title=args.job_title,
            evidence={"set_by": "stable_telegram_operator"},
            verified_at=datetime.now().astimezone().isoformat(),
        )
    )


def cmd_requester_profile_refresh(args: argparse.Namespace) -> None:
    from .lark import run_json
    from .routing import refresh_requester_profile

    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    _emit(
        refresh_requester_profile(
            conn,
            cfg,
            requester_id=args.requester_id,
            runner=partial(run_json, executable=cfg.runtime("lark_cli_command")),
        )
    )


def cmd_route_review(args: argparse.Namespace) -> None:
    from .routing import review_route

    cfg = _config(args)
    reviewer_id, _ = _control(args, cfg)
    conn = connect(cfg.database_path)
    migrate(conn)
    _emit(
        review_route(
            conn,
            route_decision_id=args.route_decision_id,
            decision=args.decision,
            reviewer_id=reviewer_id,
            note=args.note,
        )
    )


def cmd_runtime_doctor(args: argparse.Namespace) -> None:
    from .preflight import runtime_doctor

    _emit(runtime_doctor(_config(args), check_remote=args.check_remote,
                         check_cli_help=getattr(args, 'check_cli_help', False)))


def cmd_broker_deployment_doctor(args: argparse.Namespace) -> None:
    from .broker_deployment_doctor import inspect

    _emit(inspect(database=args.database, release_directory=args.release_directory,
                  catalog_directory=args.catalog_directory, control_user=args.control_user,
                  worker_user=args.worker_user))


def cmd_poll_once(args: argparse.Namespace) -> None:
    from .ingress import poll_user_messages
    from .lark import run_json

    cfg = _config(args)
    _emit(
        poll_user_messages(
            _conn(args),
            cfg,
            runner=partial(run_json, executable=cfg.runtime("lark_cli_command")),
        )
    )


def cmd_mail_poll_once(args: argparse.Namespace) -> None:
    from .ingress import poll_user_mail
    from .lark import run_mail_json

    cfg = _config(args)
    _emit(
        poll_user_mail(
            _conn(args),
            cfg,
            runner=partial(run_mail_json, executable=cfg.runtime("lark_cli_command")),
        )
    )


def cmd_control(args: argparse.Namespace) -> None:
    import shlex

    from .control import ControlMessage, execute_control

    cfg = _config(args)
    words = shlex.split(args.text)
    readonly = bool(words and words[0].lower() in {"workbench", "workbench-nav"})
    conn = _readonly_conn(args) if readonly else _conn(args)
    try:
        _emit(
            execute_control(
                conn,
                cfg,
                ControlMessage(
                    args.control_user_id,
                    args.control_chat_id,
                    args.message_id,
                    args.text,
                    source_card_message_id=getattr(args, "source_card_message_id", None),
                ),
                control_channel=getattr(args, "control_channel", "telegram"),
            )
        )
    finally:
        conn.close()


def cmd_control_callback(args: argparse.Namespace) -> None:
    from .control import (
        ControlMessage,
        execute_approval_callback,
        execute_case_callback,
    )

    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    if (
        sum(bool(value) for value in (args.panel_id, args.case_id, args.approval_id))
        != 1
    ):
        raise ValueError("callback requires exactly one target")
    if args.panel_id:
        from .approvals import verify_control_identity
        from .runtime_control import execute_global_callback

        verify_control_identity(cfg, args.control_user_id, args.control_chat_id)
        _emit(
            execute_global_callback(
                conn,
                cfg,
                action=args.action,
                panel_id=args.panel_id,
                operator_user_id=args.control_user_id,
                chat_id=args.control_chat_id,
                callback_query_id=args.callback_query_id,
                prompt_message_id=args.prompt_message_id,
            )
        )
        return
    message = ControlMessage(
        args.control_user_id,
        args.control_chat_id,
        args.callback_query_id,
        f"telegram-button:{args.action}:{args.approval_id or args.case_id}",
    )
    if args.case_id:
        _emit(
            execute_case_callback(
                conn,
                cfg,
                message,
                action=args.action,
                case_id=args.case_id,
                prompt_message_id=args.prompt_message_id,
            )
        )
        return
    if not args.approval_id:
        raise ValueError("callback requires --approval-id or --case-id")
    _emit(
        execute_approval_callback(
            conn,
            cfg,
            message,
            action=args.action,
            approval_id=args.approval_id,
            prompt_message_id=args.prompt_message_id,
        )
    )


def cmd_control_panel_bind(args: argparse.Namespace) -> None:
    from .approvals import verify_control_identity
    from .runtime_control import bind_global_panel

    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    verify_control_identity(cfg, args.control_user_id, args.control_chat_id)
    _emit(
        bind_global_panel(
            conn,
            panel_id=args.panel_id,
            operator_user_id=args.control_user_id,
            chat_id=args.control_chat_id,
            command_message_id=args.command_message_id,
            prompt_message_id=args.prompt_message_id,
        )
    )


def cmd_chat_history_backfill(args: argparse.Namespace) -> None:
    from .chat_history import backfill

    cfg = _config(args)
    root = (
        Path(args.offline_root).expanduser().resolve()
        if args.offline_root
        else cfg.data_dir
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    conn = connect(root / "state" / "review.db") if args.offline_root else _conn(args)
    migrate(conn)
    _emit(
        backfill(
            conn,
            data_dir=root,
            start_at=args.start,
            end_at=args.end,
            resume_run_id=args.resume,
            workers=args.workers,
        )
    )


def cmd_chat_history_extract(args: argparse.Namespace) -> None:
    from .chat_history import extract_candidates

    cfg = _config(args)
    owner_open_id = str(cfg.raw["identity"].get("feishu_owner_open_id") or "")
    if not owner_open_id:
        raise ValueError("feishu_owner_open_id is required for owner-answer extraction")
    report = Path(args.report).expanduser().resolve()
    root = Path(args.offline_root).expanduser().resolve() if args.offline_root else None
    conn = connect(root / "state" / "review.db") if root else _conn(args)
    migrate(conn)
    _emit(
        extract_candidates(
            conn,
            run_id=args.run_id,
            report_path=report,
            owner_open_id=owner_open_id,
            limit=args.limit,
        )
    )


def _offline_conn(args: argparse.Namespace, cfg: Config) -> sqlite3.Connection:
    root = Path(args.offline_root).expanduser().resolve() if args.offline_root else None
    conn = connect(root / "state" / "review.db") if root else connect(cfg.database_path)
    migrate(conn)
    return conn


def cmd_doc_history_backfill(args: argparse.Namespace) -> None:
    from .doc_history import backfill

    cfg = _config(args)
    root = (
        Path(args.offline_root).expanduser().resolve()
        if args.offline_root
        else cfg.data_dir
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    _emit(
        backfill(
            _offline_conn(args, cfg),
            data_dir=root,
            resume_run_id=args.resume,
            workers=args.workers,
        )
    )


def cmd_doc_history_report(args: argparse.Namespace) -> None:
    from .doc_history import build_review_report

    cfg = _config(args)
    _emit(
        build_review_report(
            _offline_conn(args, cfg),
            run_id=args.run_id,
            report_path=Path(args.report).expanduser().resolve(),
            limit=args.limit,
        )
    )


def cmd_doc_history_link_import(args: argparse.Namespace) -> None:
    from .doc_history import import_link_manifest

    cfg = _config(args)
    _emit(
        import_link_manifest(
            _offline_conn(args, cfg),
            manifest_path=Path(args.manifest).expanduser().resolve(),
            report_path=Path(args.report).expanduser().resolve(),
        )
    )


def cmd_knowledge_add(args: argparse.Namespace) -> None:
    from .knowledge import create_candidate

    knowledge_id = create_candidate(
        _conn(args),
        title=args.title,
        questions=args.question,
        answer_markdown=args.answer,
        project=args.project,
        module=args.module,
        software_version=args.software_version,
        disclosure_class=args.disclosure_class,
        confidence=args.confidence,
        source_authority=args.source_authority,
        canonical_case_id=args.case_id,
        source_digest=args.source_digest,
    )
    _emit({"knowledge_id": knowledge_id, "status": "candidate"})


def cmd_knowledge_review(args: argparse.Namespace) -> None:
    from .approvals import verify_control_identity
    from .knowledge import review

    cfg = _config(args)
    verify_control_identity(cfg, args.control_user_id, args.control_chat_id)
    conn = connect(cfg.database_path)
    migrate(conn)
    review(
        conn,
        knowledge_id=args.knowledge_id,
        reviewer_id=args.control_user_id,
        decision=args.decision,
    )
    _emit({"knowledge_id": args.knowledge_id, "status": args.decision})


def cmd_knowledge_search(args: argparse.Namespace) -> None:
    from .knowledge import search

    _emit(
        search(
            _conn(args),
            query=args.query,
            requester_id=args.requester_id,
            chat_id=args.chat_id,
            project=args.project,
            module=args.module,
            software_version=args.software_version,
            limit=args.limit,
        )
    )


def cmd_knowledge_corpus(args: argparse.Namespace) -> None:
    from .knowledge_corpus import build, state
    conn = _existing_schema_conn(args, writable=args.corpus_build)
    try:
        _emit(build(conn) if args.corpus_build else state(conn))
    finally:
        conn.close()


def cmd_knowledge_source_register(args: argparse.Namespace) -> None:
    from .knowledge import register_source

    result = register_source(
        _conn(args),
        source_type=args.source_type,
        stable_external_id=args.stable_id,
        title=args.title,
        url=args.url,
        acl=_json_arg(args.acl_json),
        source_version=args.source_version,
        content_digest=args.content_digest,
        updated_at=args.updated_at,
    )
    _emit(result)


def cmd_knowledge_source_list(args: argparse.Namespace) -> None:
    from .knowledge import list_registered_sources

    sources = list_registered_sources(
        _conn(args), source_type=args.source_type, limit=args.limit
    )
    _emit({"sources": sources, "count": len(sources)})


def cmd_knowledge_source_attach(args: argparse.Namespace) -> None:
    from .knowledge import attach_registered_source

    result = attach_registered_source(
        _conn(args),
        knowledge_id=args.knowledge_id,
        source_type=args.source_type,
        stable_external_id=args.stable_id,
        claim=args.claim,
    )
    _emit({"knowledge_id": args.knowledge_id, **result})


def cmd_knowledge_feedback(args: argparse.Namespace) -> None:
    from .knowledge import record_feedback

    _emit(
        record_feedback(
            _conn(args),
            verdict=args.verdict,
            actor_id=args.actor_id,
            knowledge_id=args.target if args.target.startswith("knw_") else None,
            case_id=args.target if args.target.startswith("K3-") else None,
            detail=args.detail,
        )
    )


def cmd_knowledge_source_refresh(args: argparse.Namespace) -> None:
    from .lark import run_json
    from .source_refresh import refresh_registered_sources

    cfg = _config(args)
    _emit(
        refresh_registered_sources(
            _conn(args),
            runner=partial(run_json, executable=cfg.runtime("lark_cli_command")),
            max_age_hours=args.max_age_hours,
            limit=args.limit,
        )
    )


def cmd_knowledge_bundle_export(args: argparse.Namespace) -> None:
    from .knowledge_bundle import export_bundle

    conn = connect(Path(args.database).expanduser().resolve())
    migrate(conn)
    _emit(export_bundle(conn, output_path=Path(args.output)))


def cmd_knowledge_bundle_plan(args: argparse.Namespace) -> None:
    from .knowledge_bundle import plan_import

    _emit(plan_import(_conn(args), bundle_path=Path(args.bundle)))


def cmd_knowledge_bundle_import(args: argparse.Namespace) -> None:
    from .approvals import verify_control_identity
    from .knowledge_bundle import import_bundle

    cfg = _config(args)
    verify_control_identity(cfg, args.control_user_id, args.control_chat_id)
    _emit(
        import_bundle(
            _conn(args),
            bundle_path=Path(args.bundle),
            approved_digest=args.approve_digest,
            reviewer_id=args.control_user_id,
        )
    )


def cmd_knowledge_repo_digest(args: argparse.Namespace) -> None:
    from .professional_knowledge import calculate_article_digest

    _emit(calculate_article_digest(Path(args.article)))


def cmd_knowledge_repo_lint(args: argparse.Namespace) -> None:
    from .professional_knowledge import lint_repository

    report = lint_repository(Path(args.root))
    _emit(
        {
            "root": report["root"],
            "article_count": report["article_count"],
            "published_count": report["published_count"],
            "articles": [
                {
                    "path": str(article.path),
                    "id": article.stable_id,
                    "revision": article.revision,
                    "status": article.metadata["status"],
                    "revision_digest": article.revision_digest,
                }
                for article in report["articles"]
            ],
        }
    )


def cmd_knowledge_repo_compile(args: argparse.Namespace) -> None:
    from .professional_knowledge import compile_repository, write_bundle

    bundle = compile_repository(Path(args.root))
    _emit(write_bundle(bundle, Path(args.output)))


def cmd_knowledge_git_verify(args: argparse.Namespace) -> None:
    from .professional_knowledge import ProfessionalKnowledgeError, verify_git_sources

    blob_reader = None
    if args.remote:
        import shlex
        import subprocess

        cfg = _config(args)
        configured = cfg.raw["repositories"].get(args.repository)
        if not isinstance(configured, dict):
            raise ValueError("repository is not configured")
        checkout = Path(str(configured["path"]))

        def blob_reader(_checkout: Path, commit: str, path: str) -> bytes:
            remote_command = " ".join(
                [
                    "git",
                    "-C",
                    shlex.quote(str(configured["path"])),
                    "show",
                    shlex.quote(f"{commit}:{path}"),
                ]
            )
            process = subprocess.run(
                command_argv(cfg, remote_command),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if process.returncode != 0:
                raise ProfessionalKnowledgeError(
                    f"cannot read remote Git blob {commit}:{path}"
                )
            return process.stdout

    else:
        if not args.checkout:
            raise ValueError("local verification requires --checkout")
        checkout = Path(args.checkout)

    _emit(
        verify_git_sources(
            Path(args.root),
            repository=args.repository,
            checkout=checkout,
            blob_reader=blob_reader,
        )
    )


def cmd_knowledge_legacy_inventory(args: argparse.Namespace) -> None:
    from .professional_knowledge import write_legacy_inventory

    database = Path(args.database).expanduser().resolve()
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _emit(write_legacy_inventory(conn, output_path=Path(args.output)))


def cmd_knowledge_legacy_draft_export(args: argparse.Namespace) -> None:
    from .professional_knowledge import export_legacy_drafts

    database = Path(args.database).expanduser().resolve()
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _emit(export_legacy_drafts(conn, repository_root=Path(args.root)))


def cmd_knowledge_professional_plan(args: argparse.Namespace) -> None:
    from .professional_knowledge import plan_import

    _emit(plan_import(_conn(args), bundle_path=Path(args.bundle)))


def cmd_knowledge_professional_import(args: argparse.Namespace) -> None:
    from .approvals import verify_control_identity
    from .professional_knowledge import import_bundle

    cfg = _config(args)
    verify_control_identity(cfg, args.control_user_id, args.control_chat_id)
    _emit(
        import_bundle(
            _conn(args),
            bundle_path=Path(args.bundle),
            approved_digest=args.approve_digest,
            reviewer_id=args.control_user_id,
        )
    )


def cmd_knowledge_evaluate(args: argparse.Namespace) -> None:
    from .knowledge_eval import evaluate

    _emit(evaluate(Path(args.gold), Path(args.predictions)))


def cmd_knowledge_gold_groups(args: argparse.Namespace) -> None:
    from .evaluation_groups import (
        group_candidates,
        parse_split_plan,
        validate_split_plan,
    )
    from .knowledge_gold_review import _load_jsonl, _private_regular_file

    candidates, candidate_digest = _load_jsonl(
        Path(args.candidates), label='candidate file',
        schema_name='knowledge-evaluation-candidate-v1.json',
    )
    grouping = group_candidates(candidates)
    if getattr(args, 'split_plan', None):
        plan = parse_split_plan(_private_regular_file(Path(args.split_plan), label='split plan'))
        _emit(validate_split_plan(grouping, plan, candidate_digest=candidate_digest))
    else:
        _emit({**grouping, 'candidate_digest': candidate_digest})


def cmd_knowledge_gold_candidates(args: argparse.Namespace) -> None:
    from .knowledge_gold import generate_gold_candidates

    database = Path(args.database).expanduser().resolve()
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _emit(
        generate_gold_candidates(
            conn,
            output_path=Path(args.output),
            repository_root=Path(args.root) if args.root else None,
            limit=args.limit,
        )
    )


def cmd_knowledge_gold_promote(args: argparse.Namespace) -> None:
    from .knowledge_gold_review import promote_reviewed_candidates

    _emit(
        promote_reviewed_candidates(
            candidates_path=Path(args.candidates),
            reviews_path=Path(args.reviews),
            output_dir=Path(args.output_dir),
            allow_partial=args.allow_partial,
        )
    )


def cmd_knowledge_gold_review_init(args: argparse.Namespace) -> None:
    from .knowledge_gold_review import initialize_gold_reviews

    _emit(
        initialize_gold_reviews(
            candidates_path=Path(args.candidates),
            output_path=Path(args.output),
        )
    )


def cmd_knowledge_gold_verify(args: argparse.Namespace) -> None:
    from .knowledge_gold_review import verify_gold_bundle

    _emit(
        verify_gold_bundle(
            Path(args.bundle),
            expected_manifest_digest=args.expected_manifest_digest,
        )
    )


def cmd_knowledge_evaluate_bundle(args: argparse.Namespace) -> None:
    from .knowledge_gold_review import evaluate_gold_bundle

    _emit(
        evaluate_gold_bundle(
            bundle_dir=Path(args.bundle),
            predictions_path=Path(args.predictions),
            approved_manifest_digest=args.approve_digest,
            candidates_path=Path(args.candidates) if getattr(args, 'candidates', None) else None,
            split_plan_path=Path(args.split_plan) if getattr(args, 'split_plan', None) else None,
            baseline_predictions_path=(
                Path(args.baseline_predictions) if args.baseline_predictions else None
            ),
        )
    )


def cmd_knowledge_release_check(args: argparse.Namespace) -> None:
    from .knowledge_release import verify_release

    conn = _readonly_conn(args)
    try:
        _emit(verify_release(conn, _config(args)))
    finally:
        conn.close()


def cmd_knowledge_release_prepare(args: argparse.Namespace) -> None:
    from .knowledge_runtime_evaluation import prepare_release_candidate, write_candidate
    from .semantic import hermes_semantic_selector
    from .semantic_budget import selector as budgeted_selector

    config = _config(args)
    conn = _readonly_conn(args)
    try:
        contexts = (
            json.loads(Path(args.request_contexts).read_text(encoding="utf-8"))
            if args.request_contexts
            else None
        )
        selector = (
            budgeted_selector(
                conn, config, hermes_semantic_selector, scope="knowledge-release-prepare"
            )
            if args.selector == "hermes"
            else None
        )
        candidate = prepare_release_candidate(
            conn,
            config,
            gold_bundle=Path(args.gold_bundle),
            approved_gold_digest=args.approve_gold_digest,
            instance_id=args.instance_id,
            key_id=args.key_id,
            contexts=contexts,
            selector=selector,
            validity_hours=args.validity_hours,
            evidence_class=args.evidence_class,
            paired_baseline=args.paired_baseline,
        )
        write_candidate(Path(args.output).expanduser(), candidate)
        _emit(
            {
                "status": candidate["status"],
                "authorized": False,
                "output": args.output,
                "candidate_digest": candidate["candidate_digest"],
                "selector": args.selector,
                "report": candidate["payload"]["evaluation"]["report"],
                "warning": "Unsigned candidate only. The selected replay runtime must match the actual live runtime.",
            }
        )
    finally:
        conn.close()


def cmd_knowledge_professional_workbench(args: argparse.Namespace) -> None:
    from .workbench import (
        professional_knowledge_snapshot,
        render_professional_knowledge,
    )

    if args.database:
        database = Path(args.database).expanduser().resolve()
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
    else:
        conn = _conn(args)
    snapshot = professional_knowledge_snapshot(
        conn,
        repository_root=Path(args.root),
        issue_limit=args.issue_limit,
    )
    if args.text:
        _emit({**snapshot, "text": render_professional_knowledge(snapshot)})
    else:
        _emit(snapshot)


def cmd_shadow_report(args: argparse.Namespace) -> None:
    from .shadow import report

    _emit(report(_conn(args), days=args.days))


def cmd_shadow_review(args: argparse.Namespace) -> None:
    from .approvals import verify_control_identity
    from .shadow import review_suggestion

    cfg = _config(args)
    verify_control_identity(cfg, args.control_user_id, args.control_chat_id)
    conn = connect(cfg.database_path)
    migrate(conn)
    _emit(
        review_suggestion(
            conn,
            suggestion_id=args.suggestion_id,
            decision=args.decision,
            reviewer_id=args.control_user_id,
            note=args.note,
        )
    )


def cmd_mail_summary(args: argparse.Namespace) -> None:
    from .lark import run_mail_json
    from .mail import latest_summary_slot, prepare_summary, refresh_missing_bodies
    from .semantic import hermes_mail_summarizer
    from .semantic_budget import selector as budgeted_selector

    cfg = _config(args)
    if cfg.mode != "active" or not cfg.feature("mail"):
        raise RuntimeError(
            "mail summary delivery requires Active mode and mail feature"
        )
    scheduled_at = args.scheduled_at or latest_summary_slot(
        args.slot,
        timezone=cfg.raw["timezone"],
    )
    from .operator_notifications import destination as notice_destination

    destination = args.destination
    notification_channel = "telegram"
    if not destination:
        target = notice_destination(cfg)
        if target is None:
            if cfg.raw.get("operator_notifications", {}).get("channel") != "web":
                raise RuntimeError("mail summary requires a configured notification destination")
            notification_channel, destination = "web", "web"
        else:
            notification_channel, destination = target
    conn = _conn(args)
    body_backfill = refresh_missing_bodies(
        conn,
        scheduled_at=scheduled_at,
        runner=partial(
            run_mail_json,
            executable=cfg.runtime("lark_cli_command"),
        ),
    )
    result = prepare_summary(
        conn,
        scheduled_at=scheduled_at,
        destination=destination,
        notification_channel=notification_channel,
        slot=args.slot,
        summarizer=budgeted_selector(
            conn, cfg, hermes_mail_summarizer,
            scope=f"mail-summary:{args.slot}:{scheduled_at}:{destination}",
        ),
        share_destination=cfg.mail("summary_share_chat_id"),
        max_important=cfg.mail("max_important_per_summary"),
        timezone=cfg.raw["timezone"],
        max_body_chars=cfg.mail("max_body_chars_per_message"),
        max_input_chars=cfg.mail("max_input_chars"),
    )
    result["body_backfill"] = body_backfill
    _emit(result)


def cmd_mail_catalog_scan(args: argparse.Namespace) -> None:
    from .lark import run_mail_json
    from .mail_catalog import scan_mail_catalog
    from .semantic import hermes_mail_classifier
    from .semantic_budget import selector as budgeted_selector

    cfg = _config(args)
    conn = _conn(args)
    _emit(
        scan_mail_catalog(
            conn,
            cfg,
            runner=partial(
                run_mail_json,
                executable=cfg.runtime("lark_cli_command"),
            ),
            classifier=budgeted_selector(
                conn, cfg, hermes_mail_classifier, scope="mail-catalog-scan",
            ),
            max_pages=args.max_pages,
            page_size=args.page_size,
            restart=args.restart,
            resume_failed=getattr(args, 'resume_failed', False),
        )
    )


def cmd_mail_catalog_overview(args: argparse.Namespace) -> None:
    from .mail_catalog import catalog_overview

    _emit(catalog_overview(_conn(args)))


def cmd_mail_summary_show(args: argparse.Namespace) -> None:
    from .mail_snapshot import query_summary

    conn = _readonly_conn(args)
    try:
        _emit(
            query_summary(
                conn,
                digest_id=args.summary_id,
                category=args.category,
                attention=args.attention,
                page=args.page,
                page_size=args.page_size,
                since=args.since,
                until=args.until,
                expected_digest=args.expected_digest,
            )
        )
    finally:
        conn.close()


def cmd_mail_category_correct(args: argparse.Namespace) -> None:
    from .mail_snapshot import correct_category

    # This local administrative CLI carries the same OS-account authority as
    # existing control commands; Telegram callers authenticate before invoking.
    actor_id = _config(args).control_operator_id
    if not actor_id:
        raise ValueError("operator identity is not configured")
    _emit(
        correct_category(
            _conn(args),
            message_id=args.message_id,
            category=args.category,
            actor_id=actor_id,
            reason=args.reason,
            expected_updated_at=args.expected_updated_at,
            external_id=args.request_id,
        )
    )


def cmd_mail_catalog_query(args: argparse.Namespace) -> None:
    from .mail_catalog import query_catalog

    _emit(
        query_catalog(
            _conn(args),
            category=args.category,
            attention=args.attention,
            topic=args.topic,
            needs_review=args.needs_review,
            limit=args.limit,
        )
    )


def cmd_release_impact(args: argparse.Namespace) -> None:
    from .release_impact import assess_release_change
    from .semantic import hermes_release_impact_analyzer
    from .semantic_budget import selector as budgeted_selector

    cfg = _config(args)
    conn = _conn(args)
    _emit(
        assess_release_change(
            conn,
            cfg,
            change=_json_arg(args.change),
            analyzer=budgeted_selector(
                conn, cfg, hermes_release_impact_analyzer, scope="release-impact",
            ),
        )
    )


def cmd_meeting_preview(args: argparse.Namespace) -> None:
    from .calendar import create_meeting_preview, normalize_meeting_action
    from .lark import run_json
    from .meeting_recovery import bind_meeting_action

    cfg = _config(args)
    action = normalize_meeting_action(
        case_id=args.case_id,
        summary=args.summary,
        start=args.start,
        end=args.end,
        attendee_ids=args.attendee_id,
        description=args.description,
        room_ids=args.room_id,
        rrule=args.rrule,
        timezone=cfg.raw["timezone"],
    )
    action = bind_meeting_action(
        cfg,
        action,
        runner=partial(run_json, executable=cfg.runtime("lark_cli_command")),
    )
    conn = _conn(args)
    try:
        _emit(create_meeting_preview(conn, action=action))
    finally:
        conn.close()


def cmd_meeting_recovery_report(args: argparse.Namespace) -> None:
    from .meeting_recovery import meeting_recovery_report

    cfg = _config(args)
    conn = _readonly_conn(args)
    try:
        _emit(
            meeting_recovery_report(
                conn,
                cfg,
                preview_id=args.preview_id,
                observation_limit=args.observation_limit,
            )
        )
    finally:
        conn.close()


def cmd_meeting_create(args: argparse.Namespace) -> None:
    from .calendar import execute_meeting_create
    from .lark import run_json

    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    _emit(
        execute_meeting_create(
            conn,
            cfg,
            preview_id=args.preview_id,
            runner=partial(run_json, executable=cfg.runtime("lark_cli_command")),
        )
    )


def cmd_base_preview(args: argparse.Namespace) -> None:
    from .base_sync import base_bootstrap_preview

    _emit(base_bootstrap_preview())


def cmd_base_recovery_report(args: argparse.Namespace) -> None:
    from .base_sync_inventory import snapshot
    conn = _readonly_conn(args)
    try:
        _emit(snapshot(conn, limit=args.limit, after_operation=args.after_operation,
                       after_legacy_job=args.after_legacy_job))
    finally:
        conn.close()


def cmd_base_sync(args: argparse.Namespace) -> None:
    from .base_sync import sync_case
    from .lark import run_json

    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    _emit(
        sync_case(
            conn,
            cfg,
            case_id=args.case_id,
            runner=partial(run_json, executable=cfg.runtime("lark_cli_command")),
        )
    )


def cmd_base_sync_entity(args: argparse.Namespace) -> None:
    from .base_sync import sync_entity
    from .lark import run_json

    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    _emit(
        sync_entity(
            conn,
            cfg,
            entity_type=args.entity_type,
            entity_id=args.entity_id,
            runner=partial(run_json, executable=cfg.runtime("lark_cli_command")),
        )
    )


def cmd_base_enqueue(args: argparse.Namespace) -> None:
    from .base_sync import enqueue_dirty_entities

    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    _emit(enqueue_dirty_entities(conn, cfg, limit=args.limit))


def cmd_backup(args: argparse.Namespace) -> None:
    from .operations import backup_database, prune_backups

    cfg = _config(args)
    result = backup_database(cfg)
    result["pruned"] = prune_backups(cfg, keep_daily=cfg.raw['policy']['backup_keep_recent'],
                                    keep_weekly=cfg.raw['policy']['backup_keep_weekly'],
                                    max_age_days=cfg.raw['policy']['backup_max_age_days'])
    _emit(result)


def cmd_restore_probe(args: argparse.Namespace) -> None:
    from .operations import restore_probe

    _emit(restore_probe(args.backup))


def cmd_recovery_inventory(args: argparse.Namespace) -> None:
    from .recovery_inventory import audit

    cfg = _config(args)
    conn = _readonly_conn(args)
    try:
        _emit(audit(conn, cfg, max_files=args.limit))
    finally:
        conn.close()


def cmd_recovery_bundle(args: argparse.Namespace) -> None:
    from .recovery_bundle import create

    _emit(create(_config(args), args.output))


def cmd_recovery_bundle_verify(args: argparse.Namespace) -> None:
    from .recovery_bundle import verify

    _emit(verify(args.directory))


def cmd_recovery_stage(args: argparse.Namespace) -> None:
    from .recovery_stage import stage

    _emit(stage(args.directory, args.output))


def cmd_draft_retention(args: argparse.Namespace) -> None:
    from .draft_retention import clear, preview

    writable = args.command == 'draft-retention-clear'
    if writable and not args.confirm_logical_delete:
        raise ValueError('explicit logical-delete confirmation is required')
    conn = _existing_schema_conn(args, writable=writable)
    try:
        if writable:
            _emit(clear(conn, candidate_id=args.candidate_id, days=args.days,
                        expected_digest=args.preview_digest, actor_id=f'local-os-uid:{os.getuid()}'))
        else:
            _emit(preview(conn, candidate_id=args.candidate_id, days=args.days))
    finally:
        conn.close()


def cmd_authoring_export(args: argparse.Namespace) -> None:
    from .knowledge_authoring import export_markdown

    conn = _readonly_conn(args)
    try:
        _emit(export_markdown(conn, candidate_id=args.candidate_id, output=args.output))
    finally:
        conn.close()


def cmd_body_retention_preview(args: argparse.Namespace) -> None:
    from .body_retention import preview

    conn = _readonly_conn(args)
    try:
        _emit(preview(conn, days=args.days, after_id=args.after_id, limit=args.limit))
    finally:
        conn.close()


def cmd_backup_retention_preview(args: argparse.Namespace) -> None:
    from .operations import prune_backups

    cfg = _config(args)
    recent = args.keep_recent if args.keep_recent is not None else cfg.raw['policy']['backup_keep_recent']
    weekly = args.keep_weekly if args.keep_weekly is not None else cfg.raw['policy']['backup_keep_weekly']
    age = args.max_age_days if args.max_age_days is not None else cfg.raw['policy']['backup_max_age_days']
    paths = prune_backups(cfg, keep_daily=recent, keep_weekly=weekly, max_age_days=age, dry_run=True)
    _emit({'read_only': True, 'candidate_paths': paths, 'count': len(paths),
           'keep_recent': recent, 'keep_weekly': weekly, 'max_age_days': age,
           'note': '仅预览当前选择，不构成删除授权；近期按份数，周备份按不同自然周保留。'})


def cmd_backup_retention_history(args: argparse.Namespace) -> None:
    from .backup_prune_audit import history

    _emit(history(_config(args), after_id=args.after_id, limit=args.limit))


def cmd_backup_retention_inspect(args: argparse.Namespace) -> None:
    from .backup_prune_audit import inspect_receipt

    _emit(inspect_receipt(_config(args), receipt_id=args.receipt_id))


def cmd_body_retention_clear(args: argparse.Namespace) -> None:
    import os

    from .body_retention import clear_unreferenced_page

    if not args.confirm_database_body_only:
        raise ValueError('explicit database-body-only confirmation is required')
    if not args.confirm_error_details:
        raise ValueError('explicit error-detail clearing confirmation is required (--confirm-error-details)')
    if not 1 <= len(args.event) <= 50:
        raise ValueError('select 1–50 preview entries')
    expected = {}
    for entry in args.event:
        event_pk, separator, snapshot = entry.partition(':')
        if (not separator or not event_pk or len(event_pk) > 100 or event_pk in expected
                or len(snapshot) != 64 or any(ch not in '0123456789abcdef' for ch in snapshot)):
            raise ValueError('each --event must be a unique EVENT_PK:PREVIEW_SHA256')
        expected[event_pk] = snapshot
    conn = _existing_schema_conn(args, writable=True)
    try:
        _emit(clear_unreferenced_page(conn, days=args.days, expected=expected,
            actor=f'local-os-uid:{os.getuid()}', after_id=args.after_id, limit=args.limit))
    finally:
        conn.close()


def cmd_retention_purge(args: argparse.Namespace) -> None:
    import os
    from . import retention_purge as purge

    action = args.purge_action
    if action == 'prepare' and not args.confirm_permanent_delete:
        raise ValueError('explicit permanent-delete confirmation required')
    if action == 'reconcile' and not args.confirm_observation:
        raise ValueError('explicit observation confirmation required')
    cfg = _config(args)
    conn = _existing_schema_conn(args, writable=action not in ('preview', 'inspect'))
    actor = f'local-os-uid:{os.getuid()}'
    try:
        if action == 'preview':
            result = purge.preview(conn, cfg, attempt_id=args.attempt_id, days=args.days)
        elif action == 'prepare':
            result = purge.prepare(conn, cfg, attempt_id=args.attempt_id, days=args.days,
                binding_digest=args.binding_digest, request_id=args.request_id, actor_id=actor,
                confirm_permanent_delete=args.confirm_permanent_delete)
        elif action == 'execute':
            result = purge.execute(conn, cfg, request_id=args.request_id, actor_id=actor)
        elif action == 'inspect':
            result = purge.inspect(conn, cfg, request_id=args.request_id, actor_id=actor)
        elif action == 'cancel':
            result = purge.cancel(conn, request_id=args.request_id, actor_id=actor)
        else:
            result = purge.reconcile(conn, cfg, request_id=args.request_id, actor_id=actor,
                decision=args.decision, observation_digest=args.observation_digest,
                confirm=args.confirm_observation)
        _emit(result)
    finally:
        conn.close()


def cmd_retention(args: argparse.Namespace) -> None:
    from .operations import apply_retention, retention_preview

    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    preview = retention_preview(conn, cfg)
    if not args.apply:
        _emit({"apply": False, "count": len(preview), "preview": preview})
        return
    _emit(
        {
            "apply": True,
            "count": len(preview),
            "result": apply_retention(conn, cfg, preview),
        }
    )


def cmd_retention_recover(args: argparse.Namespace) -> None:
    from .retention_recovery import recover

    cfg = _config(args)
    conn = connect(cfg.database_path)
    migrate(conn)
    _emit(recover(conn, cfg, args.attempt_id))


def _control_cli_path(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    adjacent = Path(sys.executable).absolute().parent / "k3-supportctl"
    if adjacent.is_file():
        return adjacent
    invoked = Path(sys.argv[0]).expanduser().resolve()
    if invoked.name == "k3-supportctl" and invoked.is_file():
        return invoked
    raise ValueError("--control-cli is required when k3-supportctl cannot be resolved")


def _hermes_home(value: str | None) -> Path:
    return (
        Path(value or os.environ.get("HERMES_HOME") or "~/.hermes")
        .expanduser()
        .resolve()
    )


def cmd_hermes_plugin_plan(args: argparse.Namespace) -> None:
    from .hermes_deploy import deployment_plan

    _emit(
        deployment_plan(
            hermes_home=_hermes_home(args.hermes_home),
            control_cli=_control_cli_path(args.control_cli),
            control_config=Path(args.config).expanduser().resolve(),
            timeout_seconds=args.timeout_seconds,
        )
    )


def cmd_hermes_plugin_install(args: argparse.Namespace) -> None:
    from .hermes_deploy import deployment_plan, install_plugin

    values = {
        "hermes_home": _hermes_home(args.hermes_home),
        "control_cli": _control_cli_path(args.control_cli),
        "control_config": Path(args.config).expanduser().resolve(),
        "timeout_seconds": args.timeout_seconds,
    }
    if not args.apply:
        _emit({"apply": False, "plan": deployment_plan(**values)})
        return
    _emit({"apply": True, "result": install_plugin(**values)})


def cmd_hermes_plugin_doctor(args: argparse.Namespace) -> None:
    from .hermes_deploy import doctor

    _emit(doctor(hermes_home=_hermes_home(args.hermes_home)))


def cmd_hermes_plugin_rollback(args: argparse.Namespace) -> None:
    from .hermes_deploy import rollback_plugin

    backup = Path(args.backup).expanduser().resolve()
    if not args.apply:
        _emit({"apply": False, "would_restore": str(backup)})
        return
    _emit({"apply": True, "result": rollback_plugin(backup_root=backup)})


def _systemd_unit_dir(value: str | None) -> Path:
    return Path(value or "~/.config/systemd/user").expanduser().resolve()


def _systemd_values(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "unit_dir": _systemd_unit_dir(args.unit_dir),
        "control_cli": _control_cli_path(args.control_cli),
        "control_config": Path(args.config).expanduser().resolve(),
        "service_path": args.service_path,
    }


def cmd_systemd_plan(args: argparse.Namespace) -> None:
    from .systemd_deploy import deployment_plan

    _emit(deployment_plan(**_systemd_values(args)))


def cmd_systemd_install(args: argparse.Namespace) -> None:
    from .systemd_deploy import deployment_plan, install_units

    values = _systemd_values(args)
    if not args.apply:
        _emit({"apply": False, "plan": deployment_plan(**values)})
        return
    _emit({"apply": True, "result": install_units(**values)})


def _deployment_replay_values(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "control_cli": _control_cli_path(args.control_cli),
        "unit_dir": _systemd_unit_dir(args.unit_dir),
        "hermes_home": _hermes_home(args.hermes_home),
        "service_path": args.service_path,
        "timeout_seconds": args.timeout_seconds,
    }


def cmd_replay_archive(args: argparse.Namespace) -> None:
    from .replay_archive import capture, replay, catalog
    from .ids import digest

    if args.archive_action == 'list':
        _emit(catalog(args.root, limit=args.limit))
        return
    if args.archive_action == 'run':
        _emit(replay(args.archive, manifest_digest=args.manifest_digest,
                     timeout=args.timeout_seconds))
        return
    payload = sys.stdin.buffer.read(262145)
    if len(payload) > 262144:
        raise ValueError('recorded request exceeds input limit')
    manifest = capture(args.database, json.loads(payload), package=args.package,
                       site_packages=args.site_packages, output=args.output,
                       timeout=args.timeout_seconds)
    _emit({'output': args.output, 'manifest_digest': digest(manifest),
           'release_authorized': False})


def cmd_recorded_replay(args: argparse.Namespace) -> None:
    from .replay_recorded import run_recorded

    # Private event/config inputs go through stdin, not shell history or argv.
    payload = sys.stdin.buffer.read(262145)
    if len(payload) > 262144:
        raise ValueError('recorded request exceeds input limit')
    request = json.loads(payload)
    result = run_recorded(
        Path(args.database), request, package=Path(args.package),
        site_packages=Path(args.site_packages), timeout=args.timeout_seconds,
        expected={'runtime': args.runtime_digest, 'snapshot': args.snapshot_digest,
                  'request': args.request_digest})
    _emit(result)


def cmd_deployment_snapshot(args: argparse.Namespace) -> None:
    from .deployment_replay import capture_snapshot, write_snapshot

    cfg = _config(args)
    snapshot = capture_snapshot(cfg, **_deployment_replay_values(args))
    output = write_snapshot(snapshot, args.output)
    _emit({"output": str(output), "snapshot_digest": snapshot["snapshot_digest"]})


def cmd_deployment_replay(args: argparse.Namespace) -> None:
    from .deployment_replay import capture_snapshot, compare_snapshot, load_snapshot

    cfg = _config(args)
    expected = load_snapshot(args.snapshot)
    current = capture_snapshot(cfg, **_deployment_replay_values(args))
    _emit(compare_snapshot(expected, current))


def cmd_systemd_doctor(args: argparse.Namespace) -> None:
    from .systemd_deploy import doctor

    _emit(doctor(**_systemd_values(args)))


def cmd_systemd_rollback(args: argparse.Namespace) -> None:
    from .systemd_deploy import rollback_units

    backup = Path(args.backup).expanduser().resolve()
    if not args.apply:
        _emit({"apply": False, "would_restore": str(backup)})
        return
    _emit({"apply": True, "result": rollback_units(backup_root=backup)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="k3-supportctl")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db").set_defaults(func=cmd_init_db)

    p = sub.add_parser("config-migration-preview")
    p.add_argument("--legacy-runtime-bin")
    p.add_argument("--legacy-home")
    p.add_argument(
        "--output", help="write a new private config copy; never overwrite or activate"
    )
    p.set_defaults(func=cmd_config_migration_preview)

    sub.add_parser(
        "readiness-report",
        help="read local health, authority and knowledge facts without activation or live probes",
    ).set_defaults(func=cmd_readiness_report)

    p = sub.add_parser(
        "knowledge-gap-report",
        help="read paginated knowledge gaps; candidates are not human-reviewed truth",
    )
    p.add_argument("--since", help="inclusive ISO-8601 timestamp with timezone")
    p.add_argument(
        "--until",
        help="exclusive ISO-8601 timestamp with timezone; pin this for pagination",
    )
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--min-repeat", type=int, default=2)
    p.add_argument(
        "--expected-digest", help="reject changed report content between pages"
    )
    p.set_defaults(func=cmd_knowledge_gap_report)

    p = sub.add_parser("ingest-event")
    p.add_argument("--source", required=True)
    p.add_argument("--identity", required=True)
    p.add_argument("--external-id", required=True)
    p.add_argument("--occurred-at", required=True)
    p.add_argument("--payload", required=True)
    p.add_argument("--sender-id")
    p.add_argument("--chat-id")
    p.add_argument("--thread-id")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("create-case")
    p.add_argument("--title", required=True)
    p.add_argument(
        "--type",
        choices=[
            "faq",
            "investigation",
            "bug",
            "incident",
            "request",
            "mail",
            "meeting",
        ],
        required=True,
    )
    p.add_argument("--severity", choices=["P0", "P1", "P2", "P3"], required=True)
    p.add_argument("--confidence", type=float, required=True)
    p.add_argument("--requester-id")
    p.add_argument("--requester-chat-id")
    p.add_argument("--disclosure-class", default="internal")
    p.add_argument("--source-event-pk")
    p.add_argument("--idempotency-key")
    p.set_defaults(func=cmd_create_case)

    p = sub.add_parser("status")
    p.add_argument("case_id", nargs="?")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("transition")
    p.add_argument("case_id")
    p.add_argument("after")
    p.add_argument("--expected-version", type=int, required=True)
    p.add_argument("--actor-type", default="system")
    p.add_argument("--actor-id")
    p.add_argument("--reason", required=True)
    p.add_argument("--idempotency-key")
    p.set_defaults(func=cmd_transition)

    p = sub.add_parser("merge-case")
    p.add_argument("case_id")
    p.add_argument("canonical_case_id")
    p.add_argument("--expected-version", type=int, required=True)
    p.add_argument("--actor-type", default="system")
    p.add_argument("--actor-id")
    p.add_argument("--reason", required=True)
    p.add_argument("--idempotency-key", required=True)
    p.set_defaults(func=cmd_merge_case)

    for name, func in (
        ("pause", cmd_pause),
        ("resume", cmd_resume),
        ("takeover", cmd_takeover),
        ("cancel", cmd_cancel),
    ):
        p = sub.add_parser(name)
        p.add_argument("case_id")
        p.add_argument("--expected-version", type=int, required=True)
        p.add_argument("--reason", required=True)
        p.add_argument("--command-id", required=True)
        p.add_argument("--control-user-id")
        p.add_argument("--control-chat-id")
        p.set_defaults(func=func)

    p = sub.add_parser("request-board")
    p.add_argument("case_id")
    p.add_argument("--session-id", required=True)
    p.add_argument("--estimated-minutes", type=int, required=True)
    p.add_argument("--request-valid-minutes", type=int, default=30)
    p.set_defaults(func=cmd_request_board)

    p = sub.add_parser("request-push")
    p.add_argument("case_id")
    p.add_argument("--repo", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--commit", action="append", required=True)
    p.add_argument("--command", action="append", required=True)
    p.add_argument("--worktree")
    p.add_argument("--valid-minutes", type=int, default=30)
    p.set_defaults(func=cmd_request_push)

    p = sub.add_parser("decide-approval")
    p.add_argument("approval_id")
    p.add_argument("--decision", choices=["approve", "deny"], required=True)
    p.add_argument("--digest", required=True)
    p.add_argument("--message-id", required=True)
    p.add_argument("--decision-text", required=True)
    p.add_argument("--control-user-id", required=True)
    p.add_argument("--control-chat-id", required=True)
    p.set_defaults(func=cmd_decide_approval)

    p = sub.add_parser("gate")
    p.add_argument("gate", choices=["board", "push"])
    p.add_argument("case_id")
    p.add_argument("--session-id")
    p.add_argument("--action")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("delegate-codex")
    p.add_argument("case_id")
    p.add_argument("--repo", action="append", required=True)
    p.add_argument("--brief", required=True)
    p.set_defaults(func=cmd_delegate_codex)

    p = sub.add_parser("delegate-code", help="Create an immutable broker task from a deployment contract")
    p.add_argument("case_id")
    p.add_argument("--repo", action="append", required=True)
    p.add_argument("--brief", required=True)
    p.add_argument("--contract-directory", required=True)
    p.add_argument("--worker-uid", type=int, required=True)
    p.set_defaults(func=cmd_delegate_code)

    p = sub.add_parser("run-codex-job")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_run_codex_job)

    p = sub.add_parser("review-codex-job")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_review_codex_job)

    p = sub.add_parser("run-hermes-review")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_run_hermes_review)

    p = sub.add_parser("board-action")
    p.add_argument("case_id")
    p.add_argument("--session-id", required=True)
    p.add_argument("--action", required=True)
    p.set_defaults(func=cmd_board_action)

    p = sub.add_parser("close-board-session")
    p.add_argument("case_id")
    p.add_argument("--session-id", required=True)
    p.set_defaults(func=cmd_close_board_session)

    p = sub.add_parser("execute-wip-push")
    p.add_argument("case_id")
    p.add_argument("--action", required=True)
    p.set_defaults(func=cmd_execute_wip_push)

    p = sub.add_parser("apply-decision")
    p.add_argument("--decision", required=True)
    p.set_defaults(func=cmd_apply_decision)

    p = sub.add_parser("enqueue-outbox")
    p.add_argument("--channel", required=True)
    p.add_argument("--action-type", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--payload", required=True)
    p.add_argument("--idempotency-key", required=True)
    p.add_argument("--case-id")
    p.set_defaults(func=cmd_enqueue)

    sub.add_parser("reconcile").set_defaults(func=cmd_reconcile)
    sub.add_parser("health").set_defaults(func=cmd_health)
    p = sub.add_parser("mail-action-status")
    p.add_argument("--message-id", required=True)
    p.set_defaults(func=cmd_mail_action_status)
    p = sub.add_parser("mail-action")
    p.add_argument("--message-id", required=True)
    p.add_argument("--action", choices=("done", "reopen", "snooze", "link_case", "unlink_case"), required=True)
    p.add_argument("--minutes", type=int)
    p.add_argument("--case-id")
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--content-digest", required=True)
    p.add_argument("--request-id", required=True)
    p.add_argument("--control-user-id", required=True)
    p.add_argument("--control-chat-id", required=True)
    p.set_defaults(func=cmd_mail_action)
    sub.add_parser("notification-status").set_defaults(func=cmd_notification_status)
    p = sub.add_parser("notification-snooze")
    timing = p.add_mutually_exclusive_group(required=True)
    timing.add_argument("--minutes", type=int)
    timing.add_argument("--night", choices=("on", "off"))
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--request-id", required=True)
    p.add_argument("--control-user-id", required=True)
    p.add_argument("--control-chat-id", required=True)
    p.set_defaults(func=cmd_notification_snooze)
    p = sub.add_parser("workbench")
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--page", type=int, default=1)
    p.add_argument(
        "--cursor", help="opaque next_cursor or previous_cursor from a workbench result"
    )
    p.add_argument(
        "--view",
        choices=[
            "all",
            "needs_me",
            "ai",
            "human",
            "waiting",
            "errors",
            "approvals",
            "knowledge",
            "closed",
        ],
        default="all",
    )
    p.add_argument("--text", action="store_true")
    p.set_defaults(func=cmd_workbench)
    sub.add_parser("mail-preflight").set_defaults(func=cmd_mail_preflight)
    sub.add_parser("office-doctor").set_defaults(func=cmd_office_doctor)
    p = sub.add_parser("scope-candidates")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--now")
    p.set_defaults(func=cmd_scope_candidates)

    p = sub.add_parser("requester-profile-show")
    p.add_argument("requester_id")
    p.set_defaults(func=cmd_requester_profile_show)

    p = sub.add_parser("requester-profile-set")
    p.add_argument("requester_id")
    p.add_argument(
        "--relationship",
        choices=[
            "supervisor",
            "dotted_supervisor",
            "peer",
            "direct_report",
            "cross_function",
            "external",
            "unknown",
        ],
        required=True,
    )
    p.add_argument(
        "--function-role",
        choices=[
            "engineering",
            "project_manager",
            "product_manager",
            "qa",
            "operations",
            "management",
            "other",
            "unknown",
        ],
        required=True,
    )
    p.add_argument("--display-name")
    p.add_argument("--department")
    p.add_argument("--job-title")
    p.add_argument("--control-user-id")
    p.add_argument("--control-chat-id")
    p.set_defaults(func=cmd_requester_profile_set)

    p = sub.add_parser("requester-profile-refresh")
    p.add_argument("requester_id")
    p.set_defaults(func=cmd_requester_profile_refresh)

    p = sub.add_parser("route-review")
    p.add_argument("route_decision_id")
    p.add_argument("--decision", choices=["accepted", "rejected"], required=True)
    p.add_argument("--note")
    p.add_argument("--control-user-id")
    p.add_argument("--control-chat-id")
    p.set_defaults(func=cmd_route_review)

    p = sub.add_parser("runtime-doctor")
    p.add_argument("--check-remote", action="store_true")
    p.set_defaults(func=cmd_runtime_doctor)
    p.add_argument('--check-cli-help', action='store_true', help='inspect Lark reply and enabled Codex exec syntax without auth, sending or execution')
    p = sub.add_parser('broker-deployment-doctor', help='read-only isolated executor deployment audit')
    p.add_argument('--database', required=True)
    p.add_argument('--release-directory', required=True)
    p.add_argument('--catalog-directory', default='/etc/k3-support/execution-catalog')
    p.add_argument('--control-user', default='k3-support-control')
    p.add_argument('--worker-user', default='k3-support-worker')
    p.set_defaults(func=cmd_broker_deployment_doctor)
    p = sub.add_parser('knowledge-authoring-export', help='export an unreviewed draft to a new private Markdown file')
    p.add_argument('--candidate-id', required=True)
    p.add_argument('--output', required=True)
    p.set_defaults(func=cmd_authoring_export)
    sub.add_parser("poll-once").set_defaults(func=cmd_poll_once)
    sub.add_parser("mail-poll-once").set_defaults(func=cmd_mail_poll_once)

    p = sub.add_parser("control")
    p.add_argument("--control-channel", choices=("telegram", "feishu"), default="telegram")
    p.add_argument("--source-card-message-id")
    p.add_argument("--control-user-id", required=True)
    p.add_argument("--control-chat-id", required=True)
    p.add_argument("--message-id", required=True)
    p.add_argument("--text", required=True)
    p.set_defaults(func=cmd_control)

    p = sub.add_parser("control-callback")
    p.add_argument("--control-user-id", required=True)
    p.add_argument("--control-chat-id", required=True)
    p.add_argument("--callback-query-id", required=True)
    p.add_argument("--prompt-message-id", required=True)
    p.add_argument(
        "--action",
        choices=[
            "approve",
            "deny",
            "details",
            "claim",
            "suggest_only",
            "delegate",
            "takeover",
            "pause",
            "global_observe",
            "global_collaborate",
            "global_auto_60",
            "global_auto_request",
            "global_auto_confirm",
            "global_pause",
            "global_stop_request",
            "global_stop_confirm",
            "global_cancel_confirmation",
            "global_details",
            "global_refresh",
            "global_workbench",
        ],
        required=True,
    )
    p.add_argument("--approval-id")
    p.add_argument("--case-id")
    p.add_argument("--panel-id")
    p.set_defaults(func=cmd_control_callback)

    p = sub.add_parser("control-panel-bind")
    p.add_argument("--control-user-id", required=True)
    p.add_argument("--control-chat-id", required=True)
    p.add_argument("--command-message-id", required=True)
    p.add_argument("--prompt-message-id", required=True)
    p.add_argument("--panel-id", required=True)
    p.set_defaults(func=cmd_control_panel_bind)

    p = sub.add_parser("chat-history-backfill")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--resume")
    p.add_argument("--offline-root")
    p.add_argument("--workers", type=int, default=4)
    p.set_defaults(func=cmd_chat_history_backfill)

    p = sub.add_parser("chat-history-extract")
    p.add_argument("run_id")
    p.add_argument("--report", required=True)
    p.add_argument("--limit", type=int, default=250)
    p.add_argument("--offline-root")
    p.set_defaults(func=cmd_chat_history_extract)

    p = sub.add_parser("doc-history-backfill")
    p.add_argument("--resume")
    p.add_argument("--offline-root")
    p.add_argument("--workers", type=int, default=4)
    p.set_defaults(func=cmd_doc_history_backfill)

    p = sub.add_parser("doc-history-report")
    p.add_argument("run_id")
    p.add_argument("--report", required=True)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--offline-root")
    p.set_defaults(func=cmd_doc_history_report)

    p = sub.add_parser("doc-history-link-import")
    p.add_argument("--manifest", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--offline-root")
    p.set_defaults(func=cmd_doc_history_link_import)

    def add_hermes_deploy_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument("--hermes-home")
        target.add_argument("--control-cli")
        target.add_argument("--timeout-seconds", type=int, default=10)

    p = sub.add_parser("hermes-plugin-plan")
    add_hermes_deploy_arguments(p)
    p.set_defaults(func=cmd_hermes_plugin_plan)

    p = sub.add_parser("hermes-plugin-install")
    add_hermes_deploy_arguments(p)
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_hermes_plugin_install)

    p = sub.add_parser("hermes-plugin-doctor")
    p.add_argument("--hermes-home")
    p.set_defaults(func=cmd_hermes_plugin_doctor)

    p = sub.add_parser("hermes-plugin-rollback")
    p.add_argument("--backup", required=True)
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_hermes_plugin_rollback)

    def add_systemd_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument("--unit-dir")
        target.add_argument("--control-cli")
        target.add_argument("--service-path")

    p = sub.add_parser("systemd-plan")
    add_systemd_arguments(p)
    p.set_defaults(func=cmd_systemd_plan)

    p = sub.add_parser("systemd-install")
    add_systemd_arguments(p)
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_systemd_install)

    def add_deployment_replay_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument("--unit-dir")
        target.add_argument("--control-cli")
        target.add_argument("--service-path")
        target.add_argument("--hermes-home")
        target.add_argument("--timeout-seconds", type=int, default=10)

    p = sub.add_parser('replay-archive-list', help='list bounded archive metadata without reading case inputs')
    p.add_argument('--root', required=True)
    p.add_argument('--limit', type=int, default=50)
    p.set_defaults(func=cmd_replay_archive, archive_action='list')
    p = sub.add_parser('replay-archive-capture', help='create private replay archive; exact JSON request on stdin')
    for flag in ('database', 'package', 'site-packages', 'output'):
        p.add_argument('--'+flag, required=True)
    p.add_argument('--timeout-seconds', type=float, default=300)
    p.set_defaults(func=cmd_replay_archive, archive_action='capture')
    p = sub.add_parser('replay-archive-run', help='replay an archive using an externally retained manifest digest')
    p.add_argument('--archive', required=True)
    p.add_argument('--manifest-digest', required=True)
    p.add_argument('--timeout-seconds', type=float, default=30)
    p.set_defaults(func=cmd_replay_archive, archive_action='run')

    p = sub.add_parser('recorded-replay', help='sandbox an explicitly bound recorded runtime; JSON request on stdin')
    p.add_argument('--database', required=True)
    p.add_argument('--package', required=True)
    p.add_argument('--site-packages', required=True)
    p.add_argument('--runtime-digest', required=True)
    p.add_argument('--snapshot-digest', required=True)
    p.add_argument('--request-digest', required=True)
    p.add_argument('--timeout-seconds', type=float, default=30)
    p.set_defaults(func=cmd_recorded_replay)

    p = sub.add_parser("deployment-snapshot")
    add_deployment_replay_arguments(p)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_deployment_snapshot)

    p = sub.add_parser("deployment-replay")
    add_deployment_replay_arguments(p)
    p.add_argument("--snapshot", required=True)
    p.set_defaults(func=cmd_deployment_replay)

    p = sub.add_parser("systemd-doctor")
    add_systemd_arguments(p)
    p.set_defaults(func=cmd_systemd_doctor)

    p = sub.add_parser("systemd-rollback")
    p.add_argument("--backup", required=True)
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_systemd_rollback)

    p = sub.add_parser("knowledge-add")
    p.add_argument("--title", required=True)
    p.add_argument("--question", action="append", required=True)
    p.add_argument("--answer", required=True)
    p.add_argument("--project")
    p.add_argument("--module")
    p.add_argument("--software-version")
    p.add_argument("--disclosure-class", default="internal")
    p.add_argument("--confidence", type=float, required=True)
    p.add_argument("--source-authority", type=float, required=True)
    p.add_argument("--source-digest", required=True)
    p.add_argument("--case-id")
    p.set_defaults(func=cmd_knowledge_add)

    p = sub.add_parser("knowledge-review")
    p.add_argument("knowledge_id")
    p.add_argument(
        "--decision", choices=["approved", "candidate", "retired"], required=True
    )
    p.add_argument("--control-user-id", required=True)
    p.add_argument("--control-chat-id", required=True)
    p.set_defaults(func=cmd_knowledge_review)

    p = sub.add_parser("knowledge-search")
    p.add_argument("query")
    p.add_argument("--requester-id")
    p.add_argument("--chat-id")
    p.add_argument("--project")
    p.add_argument("--module")
    p.add_argument("--software-version")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_knowledge_search)

    p = sub.add_parser('knowledge-corpus-status')
    p.set_defaults(func=cmd_knowledge_corpus, corpus_build=False)
    p = sub.add_parser('knowledge-corpus-build')
    p.set_defaults(func=cmd_knowledge_corpus, corpus_build=True)

    p = sub.add_parser("knowledge-source-register")
    p.add_argument("--source-type", required=True)
    p.add_argument("--stable-id", required=True)
    p.add_argument("--title")
    p.add_argument("--url")
    p.add_argument("--acl-json", default='{"visibility":"private"}')
    p.add_argument("--source-version")
    p.add_argument("--content-digest")
    p.add_argument("--updated-at")
    p.set_defaults(func=cmd_knowledge_source_register)

    p = sub.add_parser("knowledge-source-list")
    p.add_argument("--source-type")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_knowledge_source_list)

    p = sub.add_parser("knowledge-source-attach")
    p.add_argument("knowledge_id")
    p.add_argument("--source-type", required=True)
    p.add_argument("--stable-id", required=True)
    p.add_argument("--claim", required=True)
    p.set_defaults(func=cmd_knowledge_source_attach)

    p = sub.add_parser("knowledge-feedback")
    p.add_argument("target")
    p.add_argument(
        "--verdict",
        choices=["helpful", "incorrect", "incomplete", "sensitive"],
        required=True,
    )
    p.add_argument("--actor-id", required=True)
    p.add_argument("--detail")
    p.set_defaults(func=cmd_knowledge_feedback)

    p = sub.add_parser("knowledge-source-refresh")
    p.add_argument("--max-age-hours", type=int, default=24)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_knowledge_source_refresh)

    p = sub.add_parser("knowledge-bundle-export")
    p.add_argument("--database", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_knowledge_bundle_export)

    p = sub.add_parser("knowledge-bundle-plan")
    p.add_argument("--bundle", required=True)
    p.set_defaults(func=cmd_knowledge_bundle_plan)

    p = sub.add_parser("knowledge-bundle-import")
    p.add_argument("--bundle", required=True)
    p.add_argument("--approve-digest", required=True)
    p.add_argument("--control-user-id", required=True)
    p.add_argument("--control-chat-id", required=True)
    p.set_defaults(func=cmd_knowledge_bundle_import)

    p = sub.add_parser("knowledge-repo-digest")
    p.add_argument("--article", required=True)
    p.set_defaults(func=cmd_knowledge_repo_digest)

    p = sub.add_parser("knowledge-repo-lint")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_knowledge_repo_lint)

    p = sub.add_parser("knowledge-repo-compile")
    p.add_argument("--root", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_knowledge_repo_compile)

    p = sub.add_parser("knowledge-git-verify")
    p.add_argument("--root", required=True)
    p.add_argument("--repository", required=True)
    location = p.add_mutually_exclusive_group(required=True)
    location.add_argument("--checkout")
    location.add_argument("--remote", action="store_true")
    p.set_defaults(func=cmd_knowledge_git_verify)

    p = sub.add_parser("knowledge-legacy-inventory")
    p.add_argument("--database", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_knowledge_legacy_inventory)

    p = sub.add_parser("knowledge-legacy-draft-export")
    p.add_argument("--database", required=True)
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_knowledge_legacy_draft_export)

    p = sub.add_parser("knowledge-professional-plan")
    p.add_argument("--bundle", required=True)
    p.set_defaults(func=cmd_knowledge_professional_plan)

    p = sub.add_parser("knowledge-professional-import")
    p.add_argument("--bundle", required=True)
    p.add_argument("--approve-digest", required=True)
    p.add_argument("--control-user-id", required=True)
    p.add_argument("--control-chat-id", required=True)
    p.set_defaults(func=cmd_knowledge_professional_import)

    p = sub.add_parser("knowledge-evaluate")
    p.add_argument("--gold", required=True)
    p.add_argument("--predictions", required=True)
    p.set_defaults(func=cmd_knowledge_evaluate)

    p = sub.add_parser("knowledge-gold-candidates")
    p.add_argument("--database", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--root")
    p.add_argument("--limit", type=int, default=1000)
    p.set_defaults(func=cmd_knowledge_gold_candidates)

    p = sub.add_parser("knowledge-gold-review-init")
    p.add_argument("--candidates", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_knowledge_gold_review_init)

    p = sub.add_parser("knowledge-gold-groups", help="read-only candidate lineage grouping; no labels or split assignment")
    p.add_argument("--candidates", required=True)
    p.add_argument("--split-plan", help="validate a private explicit split plan without applying it")
    p.set_defaults(func=cmd_knowledge_gold_groups)

    p = sub.add_parser("knowledge-gold-promote")
    p.add_argument("--candidates", required=True)
    p.add_argument("--reviews", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--allow-partial", action="store_true")
    p.set_defaults(func=cmd_knowledge_gold_promote)

    p = sub.add_parser("knowledge-gold-verify")
    p.add_argument("--bundle", required=True)
    p.add_argument("--expected-manifest-digest")
    p.set_defaults(func=cmd_knowledge_gold_verify)

    p = sub.add_parser("knowledge-evaluate-bundle")
    p.add_argument("--candidates", help="private original candidate batch for acceptance split verification")
    p.add_argument("--split-plan", help="require gold to match exactly the acceptance split")
    p.add_argument("--bundle", required=True)
    p.add_argument("--predictions", required=True)
    p.add_argument("--baseline-predictions")
    p.add_argument("--approve-digest", required=True)
    p.set_defaults(func=cmd_knowledge_evaluate_bundle)

    p = sub.add_parser(
        "knowledge-release-check",
        help="verify signed artifact without claiming an unobserved live runtime is ready",
    )
    p.set_defaults(func=cmd_knowledge_release_check)

    p = sub.add_parser(
        "knowledge-release-prepare",
        help="replay reviewed questions; create an unsigned owner-review candidate",
    )
    p.add_argument("--gold-bundle", required=True)
    p.add_argument("--approve-gold-digest", required=True)
    p.add_argument("--instance-id", required=True)
    p.add_argument("--key-id", required=True)
    p.add_argument("--selector", choices=["lexical", "hermes"], required=True)
    p.add_argument("--request-contexts")
    p.add_argument("--validity-hours", type=int, default=24)
    p.add_argument(
        "--evidence-class",
        choices=["unreviewed", "synthetic", "human_reviewed"],
        default="unreviewed",
    )
    p.add_argument("--paired-baseline", action="store_true")
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_knowledge_release_prepare)

    p = sub.add_parser("knowledge-professional-workbench")
    p.add_argument("--root", required=True)
    p.add_argument("--database")
    p.add_argument("--issue-limit", type=int, default=100)
    p.add_argument("--text", action="store_true")
    p.set_defaults(func=cmd_knowledge_professional_workbench)

    p = sub.add_parser("shadow-report")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_shadow_report)

    p = sub.add_parser("shadow-review")
    p.add_argument("suggestion_id")
    p.add_argument("--decision", choices=["accepted", "rejected"], required=True)
    p.add_argument("--control-user-id", required=True)
    p.add_argument("--control-chat-id", required=True)
    p.add_argument("--note")
    p.set_defaults(func=cmd_shadow_review)

    p = sub.add_parser("mail-summary")
    p.add_argument("--scheduled-at")
    p.add_argument("--destination")
    p.add_argument("--slot", choices=["mail_noon", "mail_evening"], required=True)
    p.set_defaults(func=cmd_mail_summary)

    p = sub.add_parser("mail-catalog-scan")
    p.add_argument("--max-pages", type=int, default=1)
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--restart", action="store_true")
    p.add_argument('--resume-failed', action='store_true', help='explicitly resume the original failed checkpoint')
    p.set_defaults(func=cmd_mail_catalog_scan)

    sub.add_parser("mail-catalog-overview").set_defaults(func=cmd_mail_catalog_overview)

    p = sub.add_parser("mail-summary-show")
    p.add_argument("summary_id")
    p.add_argument("--category")
    p.add_argument("--attention")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--expected-digest")
    p.set_defaults(func=cmd_mail_summary_show)

    p = sub.add_parser("mail-category-correct")
    p.add_argument("message_id")
    p.add_argument("--category", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--expected-updated-at", required=True)
    p.add_argument("--request-id", required=True)
    p.set_defaults(func=cmd_mail_category_correct)

    p = sub.add_parser("mail-catalog-query")
    p.add_argument("--category")
    p.add_argument("--attention")
    p.add_argument("--topic")
    p.add_argument("--needs-review", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_mail_catalog_query)

    p = sub.add_parser("release-impact")
    p.add_argument(
        "--change",
        required=True,
        help="JSON or @file with repository, change_id, revision, subject, branch, changed_paths",
    )
    p.set_defaults(func=cmd_release_impact)

    p = sub.add_parser("meeting-preview")
    p.add_argument("case_id")
    p.add_argument("--summary", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--attendee-id", action="append", default=[])
    p.add_argument("--room-id", action="append", default=[])
    p.add_argument("--description", default="")
    p.add_argument("--rrule")
    p.set_defaults(func=cmd_meeting_preview)

    p = sub.add_parser("meeting-create")
    p.add_argument("preview_id")
    p.set_defaults(func=cmd_meeting_create)

    p = sub.add_parser("meeting-recovery-report")
    p.add_argument("preview_id")
    p.add_argument("--observation-limit", type=int, default=50)
    p.set_defaults(func=cmd_meeting_recovery_report)

    sub.add_parser("base-bootstrap-preview").set_defaults(func=cmd_base_preview)
    p = sub.add_parser('base-recovery-report')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--after-operation', default='')
    p.add_argument('--after-legacy-job', default='')
    p.set_defaults(func=cmd_base_recovery_report)
    p = sub.add_parser("base-sync-case")
    p.add_argument("case_id")
    p.set_defaults(func=cmd_base_sync)
    p = sub.add_parser("base-sync-entity")
    p.add_argument("entity_type", choices=["case", "knowledge", "mail", "health"])
    p.add_argument("entity_id")
    p.set_defaults(func=cmd_base_sync_entity)
    p = sub.add_parser("base-enqueue")
    p.add_argument("--limit", type=int, default=500)
    p.set_defaults(func=cmd_base_enqueue)

    sub.add_parser("backup").set_defaults(func=cmd_backup)
    p = sub.add_parser("restore-probe")
    p.add_argument("backup")
    p.set_defaults(func=cmd_restore_probe)
    p = sub.add_parser("recovery-inventory")
    p.add_argument("--limit", type=int, default=1000)
    p.set_defaults(func=cmd_recovery_inventory)
    p = sub.add_parser("recovery-bundle")
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_recovery_bundle)
    p = sub.add_parser("recovery-bundle-verify")
    p.add_argument("directory")
    p.set_defaults(func=cmd_recovery_bundle_verify)
    p = sub.add_parser("recovery-stage")
    p.add_argument("directory")
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_recovery_stage)
    for command in ('draft-retention-preview', 'draft-retention-clear'):
        p = sub.add_parser(command)
        p.add_argument('--candidate-id', required=True)
        p.add_argument('--days', type=int, required=True)
        if command == 'draft-retention-clear':
            p.add_argument('--preview-digest', required=True)
            p.add_argument('--confirm-logical-delete', action='store_true')
        p.set_defaults(func=cmd_draft_retention)

    p = sub.add_parser("body-retention-preview")
    p.add_argument("--days", type=int, required=True)
    p.add_argument("--after-id", default="")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_body_retention_preview)
    p = sub.add_parser('backup-retention-preview')
    p.add_argument('--keep-recent', type=int)
    p.add_argument('--keep-weekly', type=int)
    p.add_argument('--max-age-days', type=int)
    p.set_defaults(func=cmd_backup_retention_preview)
    p = sub.add_parser('backup-retention-history')
    p.add_argument('--after-id', default='')
    p.add_argument('--limit', type=int, default=50)
    p.set_defaults(func=cmd_backup_retention_history)
    p = sub.add_parser('backup-retention-inspect')
    p.add_argument('receipt_id')
    p.set_defaults(func=cmd_backup_retention_inspect)
    p = sub.add_parser('body-retention-clear')
    p.add_argument('--days', type=int, required=True)
    p.add_argument('--after-id', default='')
    p.add_argument('--limit', type=int, default=30)
    p.add_argument('--event', action='append', required=True)
    p.add_argument('--confirm-database-body-only', action='store_true')
    p.add_argument('--confirm-error-details', action='store_true',
                   help='also confirm clearing retained inbound processing error details')
    p.set_defaults(func=cmd_body_retention_clear)
    for action in ('preview', 'prepare', 'execute', 'inspect', 'cancel', 'reconcile'):
        p = sub.add_parser('retention-purge-' + action)
        p.set_defaults(func=cmd_retention_purge, purge_action=action)
        if action in ('preview', 'prepare'):
            p.add_argument('--attempt-id', required=True)
            p.add_argument('--days', type=int, required=True)
        if action != 'preview':
            p.add_argument('--request-id', required=True)
        if action == 'prepare':
            p.add_argument('--binding-digest', required=True)
            p.add_argument('--confirm-permanent-delete', action='store_true')
        if action == 'reconcile':
            p.add_argument('--decision', choices=('keep_file', 'confirm_absence'), required=True)
            p.add_argument('--observation-digest', required=True)
            p.add_argument('--confirm-observation', action='store_true')
    p = sub.add_parser("retention")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_retention)
    p = sub.add_parser("retention-recover")
    p.add_argument("attempt_id")
    p.set_defaults(func=cmd_retention_recover)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary must return structured errors
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
