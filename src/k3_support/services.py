from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

from .cli import DEFAULT_CONFIG
from .config import load_config
from .db import connect, migrate, transaction
from .delivery import DeliverySuppressed, claim_outbox, deliver_claimed
from .ids import canonical_json, new_id
from .ingress import ingest_bot_value, poll_user_mail, poll_user_messages
from .lark import EventConsumer, LarkError, run_json, run_mail_json
from .operations import heartbeat, reconcile
from .operator_notifications import destination as notice_destination
from .operator_notifications import enqueue as enqueue_notice
from .orchestrator import claim_inbound, process_inbound
from .runtime_control import capability_allowed, current_global_state, outbox_eligible
from .store import claim_jobs
from .timeutil import iso_now, parse_iso


class Stop:
    requested = False


class JobHeartbeatError(RuntimeError):
    pass


JOB_POOLS = {"query": ("retrieve",), "debug": ("codex", "push"), "sync": ("base_sync",)}


def _job_worker_component(worker_id):
    if worker_id.startswith("pool/"):
        pool = worker_id.split("/", 2)[1]
        if pool not in JOB_POOLS:
            raise JobHeartbeatError("unknown_worker_pool")
        return "job_worker:" + pool
    return "job_worker"


def _job_worker_heartbeat(
    conn,
    worker_id: str,
    status: str,
    detail: dict,
    *,
    register: bool = False,
    now: datetime | None = None,
) -> bool:
    """Only the registered process instance may publish job-worker health."""
    stamp = (now or datetime.now(UTC)).isoformat()
    payload = canonical_json({**detail, "worker_id": worker_id})
    component = _job_worker_component(worker_id)
    if register:
        guard = ""
        extra = ()
        if component != "job_worker":
            guard = """ WHERE service_state.status='stopped' OR service_state.heartbeat_at<?
                OR json_extract(service_state.detail_json,'$.worker_id')=?"""
            extra = (((now or datetime.now(UTC)) - timedelta(minutes=3)).isoformat(), worker_id)
        changed = conn.execute(
            """INSERT INTO service_state(component,pid,started_at,heartbeat_at,status,detail_json)
               VALUES(?,?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET
                 pid=excluded.pid,started_at=excluded.started_at,
                 heartbeat_at=excluded.heartbeat_at,status=excluded.status,
                 detail_json=excluded.detail_json""" + guard,
            (component, os.getpid(), stamp, stamp, status, payload, *extra),
        )
        return changed.rowcount == 1
    return (
        conn.execute(
            """UPDATE service_state SET heartbeat_at=?,status=?,detail_json=?
           WHERE component=? AND json_extract(detail_json,'$.worker_id')=?""",
            (stamp, status, payload, component, worker_id),
        ).rowcount
        == 1
    )


def _renew_job_health(
    conn,
    *,
    job_id: str,
    worker_id: str,
    attempt_no: int,
    lease_seconds: int = 120,
    now: datetime | None = None,
) -> str:
    """Renew one still-live claim and its service status in the same transaction."""
    observed = now or datetime.now(UTC)
    with transaction(conn):
        service = conn.execute(
            "SELECT detail_json FROM service_state WHERE component=?",
            (_job_worker_component(worker_id),),
        ).fetchone()
        detail = json.loads(service[0]) if service else {}
        if (
            detail.get("worker_id") != worker_id
            or detail.get("job_id") != job_id
            or detail.get("heartbeat_phase") not in {"starting", "running", "finishing"}
        ):
            raise JobHeartbeatError("superseded_worker")
        job = conn.execute(
            "SELECT job_type,case_id,state,lease_owner,lease_expires_at,attempt_no FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if job is None or job["attempt_no"] != attempt_no:
            raise JobHeartbeatError("lost_job_claim")
        if job["state"] == "running":
            if (
                job["lease_owner"] != worker_id
                or not job["lease_expires_at"]
                or parse_iso(job["lease_expires_at"]) <= observed
            ):
                raise JobHeartbeatError("lost_job_claim")
            conn.execute(
                "UPDATE jobs SET heartbeat_at=?,lease_expires_at=?,updated_at=? WHERE job_id=?",
                (
                    observed.isoformat(),
                    (observed + timedelta(seconds=lease_seconds)).isoformat(),
                    observed.isoformat(),
                    job_id,
                ),
            )
            phase = "running"
        elif job["state"] in {"succeeded", "failed"} and job["lease_owner"] in {
            None,
            worker_id,
        }:
            # Executors persist their result before bounded review/cleanup.
            # Keep the worker visible without reviving a completed job lease.
            phase = "finishing"
        else:
            raise JobHeartbeatError("lost_job_claim")
        _job_worker_heartbeat(
            conn,
            worker_id,
            "ready",
            {
                "job_id": job_id,
                "job_type": job["job_type"],
                "case_id": job["case_id"],
                "attempt_no": attempt_no,
                "job_state": job["state"],
                "heartbeat_phase": phase,
            },
            now=observed,
        )
    return phase


class JobLeaseHeartbeat:
    """Callable stop handle whose failures remain visible to the executor."""

    def __init__(self) -> None:
        self.stopped = threading.Event()
        self.failed = threading.Event()
        self.error_class: str | None = None
        self.reason: str | None = None
        self.thread: threading.Thread | None = None

    def __call__(self) -> None:
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                self.error_class = "HeartbeatStopTimeout"
                self.reason = "heartbeat_thread_unresponsive"
                self.failed.set()


def _start_job_lease_heartbeat(
    database_path: Path,
    *,
    job_id: str,
    worker_id: str,
    attempt_no: int = 1,
    interval_seconds: float = 30,
    lease_seconds: int = 120,
) -> JobLeaseHeartbeat:
    """Keep a claimed job live while a blocking executor owns the service thread."""
    monitor = JobLeaseHeartbeat()

    def fail(exc: Exception) -> None:
        monitor.error_class = type(exc).__name__
        monitor.reason = (
            str(exc) if isinstance(exc, JobHeartbeatError) else "heartbeat_error"
        )
        monitor.failed.set()
        # A broken renewal connection may be unusable. Best-effort health on a
        # new connection is owner-fenced and excludes exception text/secrets.
        error_conn = None
        try:
            error_conn = connect(database_path)
            with transaction(error_conn):
                row = error_conn.execute(
                    "SELECT detail_json FROM service_state WHERE component=?",
                    (_job_worker_component(worker_id),),
                ).fetchone()
                current = json.loads(row[0]) if row else {}
                if (
                    current.get("job_id") == job_id
                    and current.get("attempt_no") == attempt_no
                ):
                    _job_worker_heartbeat(
                        error_conn,
                        worker_id,
                        "degraded",
                        {
                            **current,
                            "heartbeat_phase": monitor.reason,
                            "error_class": monitor.error_class,
                        },
                    )
        except Exception:  # noqa: BLE001, S110 - best-effort reporting must not mask the monitor's sticky failure
            pass
        finally:
            if error_conn is not None:
                error_conn.close()

    initial_conn = None
    try:
        initial_conn = connect(database_path)
        if not _job_worker_heartbeat(
            initial_conn,
            worker_id,
            "ready",
            {
                "job_id": job_id,
                "attempt_no": attempt_no,
                "heartbeat_phase": "starting",
            },
        ):
            raise JobHeartbeatError("superseded_worker")
        _renew_job_health(
            initial_conn,
            job_id=job_id,
            worker_id=worker_id,
            attempt_no=attempt_no,
            lease_seconds=lease_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - every lease initialization failure must stop the job safely
        fail(exc)
        return monitor
    finally:
        if initial_conn is not None:
            initial_conn.close()

    def renew() -> None:
        heartbeat_conn = None
        try:
            heartbeat_conn = connect(database_path)
            while not monitor.stopped.wait(interval_seconds):
                _renew_job_health(
                    heartbeat_conn,
                    job_id=job_id,
                    worker_id=worker_id,
                    attempt_no=attempt_no,
                    lease_seconds=lease_seconds,
                )
        except Exception as exc:  # noqa: BLE001 - heartbeat thread failure must fence the job and remain degraded
            fail(exc)
        finally:
            if heartbeat_conn is not None:
                heartbeat_conn.close()

    thread = threading.Thread(
        target=renew,
        name=f"k3-job-lease-{job_id}",
        daemon=True,
    )
    monitor.thread = thread
    thread.start()
    return monitor


def _handle_stop(_signum: int, _frame: object) -> None:
    Stop.requested = True


def _run(component: str, tick, interval: float = 5.0) -> None:
    Stop.requested = False
    parser = argparse.ArgumentParser(prog=f"k3-support-{component}")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    conn = connect(cfg.database_path)
    migrate(conn)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    while not Stop.requested:
        result = tick(conn, cfg)
        print(
            json.dumps(
                {"component": component, "at": iso_now(), **result}, ensure_ascii=False
            ),
            flush=True,
        )
        if args.once:
            return
        deadline = time.monotonic() + interval
        while not Stop.requested and time.monotonic() < deadline:
            time.sleep(min(0.25, deadline - time.monotonic()))


class _IngressStopped(RuntimeError):
    pass


def _ingress_poll_stream(
    cfg,
    *,
    component: str,
    poller,
    runner,
    stop_event: threading.Event,
    once: bool = False,
    interval_seconds: float = 60,
    emit=print,
) -> None:
    """One independently scheduled adapter with its own SQLite connection.

    Network waits hold no database transaction. A slow mail adapter cannot
    delay the chat/operator schedule or the supervisor's stop observation.
    """
    conn = connect(cfg.database_path)

    def stopped() -> bool:
        return stop_event.is_set() or not capability_allowed(conn, cfg, "ingest")

    def tracked_runner(argv):
        if stopped():
            raise _IngressStopped()
        heartbeat(conn, component, "ready", {"phase": "polling", "adapter": component})
        value = runner(argv)
        if stopped():
            raise _IngressStopped()
        return value

    try:
        while not stop_event.is_set():
            started = time.monotonic()
            runtime = current_global_state(conn, cfg)
            if not capability_allowed(conn, cfg, "ingest"):
                output = {"state": "stopped", "complete": False}
            else:
                try:
                    kwargs = {"runner": tracked_runner}
                    if component == "ingress_mail":
                        kwargs["should_stop"] = stopped
                    output = poller(conn, cfg, **kwargs)
                except _IngressStopped:
                    output = {"state": "stopped", "complete": False}
                except Exception as exc:  # noqa: BLE001 - isolate any poller failure without terminating unrelated ingress threads
                    output = {
                        "state": "failed",
                        "error": type(exc).__name__,
                        "message": str(exc),
                        "complete": False,
                    }
            if stop_event.is_set():
                break
            healthy = output["state"] in {"ready", "catching_up", "stopped"}
            output.update(
                {
                    "ready": healthy,
                    "mode": cfg.mode,
                    "runtime_mode": runtime["mode"],
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
            )
            heartbeat(conn, component, "ready" if healthy else "failed", output)
            emit(
                json.dumps(
                    {"component": component, "at": iso_now(), **output},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if once:
                break
            # Normal polling is start-to-start, not duration + 60 seconds.
            # Catch-up continues promptly while remaining independent of chat.
            deadline = started + (
                0.1 if output["state"] == "catching_up" else interval_seconds
            )
            while not stop_event.is_set() and time.monotonic() < deadline:
                stop_event.wait(min(0.25, max(0, deadline - time.monotonic())))
    finally:
        conn.close()


def ingress_main() -> None:
    Stop.requested = False
    parser = argparse.ArgumentParser(prog="k3-support-ingress")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--bot-only",
        action="store_true",
        help="consume bot events without polling the user account or mail",
    )
    args = parser.parse_args()
    if args.once and args.bot_only:
        parser.error("--bot-only cannot be combined with --once")
    cfg = load_config(args.config)
    lark_runner = partial(run_json, executable=cfg.runtime("lark_cli_command"))
    mail_runner = partial(run_mail_json, executable=cfg.runtime("lark_cli_command"))
    conn = connect(cfg.database_path)
    migrate(conn)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    consumers: list[EventConsumer] = []
    poll_stop = threading.Event()

    def bot_stream() -> None:
        stream_conn = connect(cfg.database_path)
        migrate(stream_conn)
        backoff = 5
        while not Stop.requested:
            consumer = EventConsumer(executable=cfg.runtime("lark_cli_command"))
            consumers.append(consumer)
            try:
                consumer.start()
                heartbeat(
                    stream_conn,
                    "ingress_bot",
                    "ready",
                    {"event_key": "im.message.receive_v1"},
                )
                for value in consumer.events():
                    if Stop.requested:
                        break
                    backoff = 5
                    ingest_bot_value(
                        stream_conn, cfg, value,
                        control_only=not capability_allowed(stream_conn, cfg, "ingest"),
                    )
                if not Stop.requested:
                    raise LarkError(
                        "event consumer ended unexpectedly", error_type="stream"
                    )
            except Exception as exc:  # noqa: BLE001 - stream supervisor classifies failures via heartbeat
                heartbeat(
                    stream_conn,
                    "ingress_bot",
                    "failed",
                    {"error": type(exc).__name__, "message": str(exc)},
                )
                deadline = time.monotonic() + backoff
                while not Stop.requested and time.monotonic() < deadline:
                    time.sleep(0.25)
                backoff = min(300, backoff * 3)
            finally:
                try:
                    consumer.stop()
                except Exception as exc:  # noqa: BLE001 - supervisor records shutdown failure
                    heartbeat(
                        stream_conn,
                        "ingress_bot",
                        "failed",
                        {"error": "shutdown_failed", "message": str(exc)},
                    )
                if consumer in consumers:
                    consumers.remove(consumer)

    threads: list[threading.Thread] = []
    if not args.once:
        threads.append(
            threading.Thread(target=bot_stream, name="k3-lark-bot", daemon=True)
        )
    adapters = [] if args.bot_only else [("ingress_poll", poll_user_messages, lark_runner)]
    if not args.bot_only and cfg.feature("mail"):
        adapters.append(("ingress_mail", poll_user_mail, mail_runner))
    poll_threads = [
        threading.Thread(
            target=_ingress_poll_stream,
            name=f"k3-{component}",
            daemon=True,
            kwargs={
                "cfg": cfg,
                "component": component,
                "poller": poller,
                "runner": runner,
                "stop_event": poll_stop,
                "once": args.once,
            },
        )
        for component, poller, runner in adapters
    ]
    threads.extend(poll_threads)
    try:
        for thread in threads:
            thread.start()
        while not Stop.requested:
            if args.once and not any(thread.is_alive() for thread in poll_threads):
                break
            if not args.once and any(not thread.is_alive() for thread in poll_threads):
                raise RuntimeError("an ingress polling thread stopped unexpectedly")
            time.sleep(0.1)
    finally:
        poll_stop.set()
        for consumer in tuple(consumers):
            consumer.stop(timeout=1)
        # The CLI adapter already bounds individual read calls to 30 seconds.
        # Do not wait for those reads to finish before acknowledging shutdown;
        # a returning poller checks poll_stop before ingest/cursor advancement.
        deadline = time.monotonic() + 0.5
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        conn.close()


def worker_main() -> None:
    # PID alone is not an ownership identity: it may be reused after a crash.
    worker_id = f"worker:{os.getpid()}:{new_id('run')}"
    registered = False
    health_fault = None

    def tick(conn, cfg):
        nonlocal registered, health_fault
        from .inbound_claims import InboundClaimLost, publish_worker_health
        from .semantic import (
            hermes_case_similarity,
            hermes_clarification_reviewer,
            hermes_diagnostic_extractor,
            hermes_meeting_planner,
            hermes_message_router,
            hermes_semantic_selector,
        )

        if not registered:
            publish_worker_health(
                conn, worker_id, "ready", {"heartbeat_phase": "idle"}, register=True
            )
            registered = True
        elif not publish_worker_health(
            conn,
            worker_id,
            "degraded" if health_fault else "ready",
            {"heartbeat_phase": "idle", "last_failure": health_fault},
        ):
            return {"ready": False, "reason": "superseded_worker"}
        from .semantic_budget import selector as budgeted_selector
        lark_runner = partial(run_json, executable=cfg.runtime("lark_cli_command"))
        runtime = current_global_state(conn, cfg)
        ids = (
            claim_inbound(conn, worker_id=worker_id, limit=1)
            if capability_allowed(conn, cfg, "triage")
            else []
        )
        results = []
        discarded = 0
        for event_pk in ids:
            try:
                results.append(
                    process_inbound(
                        conn,
                        event_pk=event_pk,
                        worker_id=worker_id,
                        config=cfg,
                        semantic_selector=budgeted_selector(
                            conn, cfg, hermes_semantic_selector, scope=event_pk
                        ),
                        message_router=budgeted_selector(
                            conn, cfg, hermes_message_router, scope=event_pk
                        ),
                        clarification_reviewer=budgeted_selector(
                            conn, cfg, hermes_clarification_reviewer, scope=event_pk
                        ),
                        similarity_selector=budgeted_selector(
                            conn, cfg, hermes_case_similarity, scope=event_pk
                        ),
                        diagnostic_extractor=budgeted_selector(
                            conn, cfg, hermes_diagnostic_extractor, scope=event_pk
                        ),
                        meeting_planner=budgeted_selector(
                            conn, cfg, hermes_meeting_planner, scope=event_pk
                        ),
                        contact_runner=lark_runner,
                        calendar_runner=lark_runner,
                        stop_requested=lambda: Stop.requested,
                        report_worker_health=True,
                    )
                )
                if results[-1].get("processed"):
                    health_fault = None
            except InboundClaimLost as exc:
                # process_inbound fences every downstream write. A late call
                # must not clear the successor's reservation or poison retry.
                discarded += 1
                if exc.error_class is not None:
                    health_fault = {
                        "reason": "heartbeat_error",
                        "error_class": exc.error_class,
                    }
            except Exception as exc:  # noqa: BLE001 - token-fenced processing failures must stay visible in worker health
                # The processing wrapper already applied token-fenced failure
                # handling, including for CLI callers. No raw fallback update.
                results.append({"processed": False, "error_class": type(exc).__name__})
                health_fault = {
                    "reason": "processing_failed",
                    "error_class": type(exc).__name__,
                }
        depth = conn.execute(
            "SELECT count(*) FROM inbound_events WHERE status='new'"
        ).fetchone()[0]
        detail = {
            "processed": sum(bool(result.get("processed")) for result in results),
            "discarded": discarded,
            "inbox_depth": depth,
            "mode": cfg.mode,
            "runtime_mode": runtime["mode"],
            "heartbeat_phase": "idle" if health_fault is None else "failed",
            "last_failure": health_fault,
        }
        owned = publish_worker_health(
            conn, worker_id, "ready" if health_fault is None else "degraded", detail
        )
        return {"ready": owned and health_fault is None, **detail}

    _run("worker", tick, 5)


def job_worker_main(pool: str = "all") -> None:
    if pool != "all" and pool not in JOB_POOLS:
        raise JobHeartbeatError("unknown_worker_pool")
    worker_id = f"job-worker:{os.getpid()}:{new_id('run')}"
    if pool != "all":
        worker_id = f"pool/{pool}/{worker_id}"
    registered = False
    health_fault = None

    def tick(conn, cfg):
        nonlocal registered, health_fault
        from .base_sync import fail_base_sync_job, run_base_sync_job
        from .base_sync_attempt import AttemptRef
        from .executors import BoardExecutor, record_codex_result, run_codex_job
        from .retrieval import run_retrieval_job
        from .review import (
            fail_wip_push_job,
            handle_codex_completion,
            run_wip_push_job,
        )
        from .store import transition_case

        if health_fault is not None:
            _job_worker_heartbeat(
                conn,
                worker_id,
                "degraded",
                {
                    "heartbeat_phase": "heartbeat_error",
                    **health_fault,
                },
            )
            return {"ready": False, **health_fault}
        if not _job_worker_heartbeat(
            conn,
            worker_id,
            "ready",
            {"heartbeat_phase": "idle"},
            register=not registered,
        ):
            return {"ready": False, "error": "superseded_worker"}
        registered = True
        lark_runner = partial(run_json, executable=cfg.runtime("lark_cli_command"))
        if pool in {"all", "sync"}:
            from .mail_meeting_prepare import dispatch_one as prepare_mail_meeting
            from .meeting_dispatch import dispatch_one

            prepared = prepare_mail_meeting(conn, cfg, runner=lark_runner)
            if prepared is not None:
                return {"ready": True, "mail_meeting_prepare": prepared}
            meeting = dispatch_one(conn, cfg, runner=lark_runner)
            if meeting is not None:
                return {"ready": True, "meeting_dispatch": meeting}
        runtime = current_global_state(conn, cfg)
        allowed_job_types = (
            ["retrieve"] if capability_allowed(conn, cfg, "retrieve") else []
        )
        if cfg.feature("codex") and capability_allowed(conn, cfg, "codex"):
            allowed_job_types.append("codex")
        if cfg.feature("wip_push") and capability_allowed(conn, cfg, "wip_push"):
            allowed_job_types.append("push")
        if cfg.feature("base_sync") and capability_allowed(conn, cfg, "base_sync"):
            allowed_job_types.append("base_sync")
        if pool != "all":
            allowed_job_types = [kind for kind in allowed_job_types if kind in JOB_POOLS[pool]]
        claimed = (
            claim_jobs(
                conn,
                worker_id,
                limit=1,
                lease_seconds=120,
                job_types=tuple(allowed_job_types),
            )
            if allowed_job_types
            else []
        )
        result: dict[str, object] = {
            "claimed": len(claimed),
            "mode": cfg.mode,
            "runtime_mode": runtime["mode"],
            "allowed_job_types": allowed_job_types,
            "execution_enabled": True,
        }
        stop_lease_heartbeat = None

        def finish_result():
            nonlocal health_fault
            if stop_lease_heartbeat is not None:
                stop_lease_heartbeat()
                if stop_lease_heartbeat.failed.is_set():
                    result["heartbeat_error"] = {
                        "reason": stop_lease_heartbeat.reason,
                        "error_class": stop_lease_heartbeat.error_class,
                    }
                    health_fault = {
                        "job_id": result.get("job_id"),
                        "heartbeat_error": result["heartbeat_error"],
                    }
            healthy = "error" not in result and "heartbeat_error" not in result
            detail = {**result, "heartbeat_phase": "idle" if healthy else "degraded"}
            published = _job_worker_heartbeat(
                conn, worker_id, "ready" if healthy else "degraded", detail
            )
            return {"ready": healthy and published, **result}

        if claimed:
            job = claimed[0]
            result["job_id"] = job["job_id"]
            stop_lease_heartbeat = _start_job_lease_heartbeat(
                cfg.database_path,
                job_id=str(job["job_id"]),
                worker_id=worker_id,
                attempt_no=int(job["attempt_no"]),
            )
            if stop_lease_heartbeat.failed.is_set():
                return finish_result()
            base_attempt = AttemptRef.from_claim(job) if job['job_type'] == 'base_sync' else None
            try:
                if job["job_type"] == "retrieve":
                    retrieval = run_retrieval_job(
                        conn, cfg, job_id=job["job_id"], runner=lark_runner
                    )
                    from .orchestrator import (
                        complete_retrieval_route,
                    )
                    from .routing import latest_route
                    from .semantic import (
                        hermes_clarification_reviewer,
                        hermes_research_link_selector,
                    )
                    from .semantic_budget import selector as budgeted_selector

                    route = latest_route(conn, str(job["case_id"]))
                    if route is not None and route["route"] == "research":
                        continuation = complete_retrieval_route(
                            conn,
                            cfg,
                            case_id=str(job["case_id"]),
                            retrieval_result=retrieval,
                            selector=budgeted_selector(
                                conn, cfg, hermes_research_link_selector,
                                scope=job["job_id"], case_id=str(job["case_id"]),
                            ),
                            clarification_reviewer=budgeted_selector(
                                conn, cfg, hermes_clarification_reviewer,
                                scope=job["job_id"], case_id=str(job["case_id"]),
                            ),
                        )
                    else:
                        continuation = complete_retrieval_route(
                            conn,
                            cfg,
                            case_id=str(job["case_id"]),
                            retrieval_result=retrieval,
                        )
                    result.update(
                        {
                            "job_id": job["job_id"],
                            "state": "succeeded",
                            "retrieval": retrieval,
                            "codex_continuation": continuation,
                        }
                    )
                    result["job_depth"] = conn.execute(
                        "SELECT count(*) FROM jobs WHERE state='queued'"
                    ).fetchone()[0]
                    return finish_result()
                if job["job_type"] == "base_sync":
                    mirrored = run_base_sync_job(
                        conn, cfg, job_id=job["job_id"], attempt_ref=base_attempt, runner=lark_runner
                    )
                    result.update(
                        {
                            "job_id": job["job_id"],
                            "state": "succeeded",
                            "base_sync": mirrored,
                        }
                    )
                    if mirrored.get('superseded'):
                        result['state'] = 'superseded'
                    result["job_depth"] = conn.execute(
                        "SELECT count(*) FROM jobs WHERE state='queued'"
                    ).fetchone()[0]
                    return finish_result()
                if job["job_type"] == "push":
                    push = run_wip_push_job(conn, cfg, job_id=job["job_id"])
                    result.update(
                        {
                            "job_id": job["job_id"],
                            "state": "succeeded",
                            "push": push,
                        }
                    )
                    if not push["review"]["ok"]:
                        result["error"] = push["review"]["error"]
                    result["job_depth"] = conn.execute(
                        "SELECT count(*) FROM jobs WHERE state='queued'"
                    ).fetchone()[0]
                    return finish_result()
                if job["job_type"] != "codex":
                    raise ValueError(f"unsupported queued job type: {job['job_type']}")
                context = json.loads(job["context_json"])
                board_session_id = context.get("board_session_id")
                execution_error: Exception | None = None
                execution = None
                try:
                    execution = run_codex_job(
                        conn,
                        cfg,
                        job_id=job["job_id"],
                        should_stop=lambda: (
                            Stop.requested
                            or stop_lease_heartbeat.failed.is_set()
                            or conn.execute(
                                "SELECT state FROM cases WHERE case_id=?",
                                (job["case_id"],),
                            ).fetchone()[0]
                            not in {
                                "triage",
                                "investigating",
                                "waiting_board",
                                "board_testing",
                                "waiting_push",
                                "monitoring",
                            }
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - cleanup must run before propagating
                    execution_error = exc
                finally:
                    if isinstance(board_session_id, str):
                        BoardExecutor(cfg).close_session(
                            conn,
                            case_id=job["case_id"],
                            session_id=board_session_id,
                        )
                        case = conn.execute(
                            "SELECT state,version FROM cases WHERE case_id=?",
                            (job["case_id"],),
                        ).fetchone()
                        if case["state"] == "board_testing":
                            transition_case(
                                conn,
                                case_id=job["case_id"],
                                after="investigating",
                                actor_type="system",
                                actor_id=worker_id,
                                reason="board session closed with fresh BROM evidence",
                                expected_version=case["version"],
                                idempotency_key=f"job:{job['job_id']}:board-closed",
                            )
                        conn.execute(
                            """UPDATE cases SET active_job_id=NULL,active_session_id=NULL,updated_at=?
                               WHERE case_id=? AND active_job_id=?""",
                            (iso_now(), job["case_id"], job["job_id"]),
                        )
                if execution_error is not None:
                    raise execution_error
                if execution is None:
                    raise RuntimeError("Codex execution returned no result")
                sections = record_codex_result(conn, job_id=job["job_id"])
                review = handle_codex_completion(conn, cfg, job_id=job["job_id"])
                result.update(
                    {
                        "job_id": job["job_id"],
                        "state": "succeeded",
                        "codex_status": sections["status"],
                        "output_digest": execution.output_digest,
                        "review": review,
                    }
                )
                if not review["ok"]:
                    result["error"] = review["error"]
            except Exception as exc:  # noqa: BLE001 - worker records terminal job error and remains available
                if job["job_type"] == "push":
                    failure = fail_wip_push_job(
                        conn, cfg, job_id=job["job_id"], error=exc
                    )
                    result.update(
                        {
                            "job_id": job["job_id"],
                            "state": "failed",
                            "error": failure["error"],
                            "push_failure": failure,
                        }
                    )
                    current = None
                elif job["job_type"] == "base_sync":
                    failure = fail_base_sync_job(conn, job_id=job["job_id"], attempt_ref=base_attempt, error=exc)
                    result.update(
                        {
                            "job_id": job["job_id"],
                            "state": failure["state"],
                            "error": f"{type(exc).__name__}: {exc}",
                            "base_sync_failure": failure,
                        }
                    )
                    current = None
                elif job["job_type"] == "retrieve":
                    from .orchestrator import continue_failed_retrieval

                    continuation = continue_failed_retrieval(
                        conn,
                        cfg,
                        error_class=type(exc).__name__,
                        job_id=str(job["job_id"]),
                        expected_attempt_no=int(job["attempt_no"]),
                    )
                    current = conn.execute(
                        "SELECT state FROM jobs WHERE job_id=?", (job["job_id"],)
                    ).fetchone()
                    result["codex_continuation"] = continuation
                else:
                    current = conn.execute(
                        "SELECT state FROM jobs WHERE job_id=?", (job["job_id"],)
                    ).fetchone()
                recorded = conn.execute(
                    "SELECT 1 FROM case_events WHERE idempotency_key=?",
                    (f"job:{job['job_id']}:recorded",),
                ).fetchone()
                if (
                    current is not None
                    and current["state"] in {"running", "succeeded"}
                    and recorded is None
                ):
                    from .job_failure import fail_attempt

                    fail_attempt(conn, job_id=job['job_id'], attempt_no=int(job['attempt_no']),
                                 error_class=type(exc).__name__)
                if job["job_type"] != "push":
                    result.update(
                        {
                            "job_id": job["job_id"],
                            "state": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
            finally:
                stop_lease_heartbeat()
        depth = conn.execute(
            "SELECT count(*) FROM jobs WHERE state='queued'"
        ).fetchone()[0]
        result["job_depth"] = depth
        return finish_result()

    _run("job-worker", tick, 5)


def _outbox_tick(conn, cfg, *, delivery=deliver_claimed):
    """Run one deterministic delivery iteration for services and canary tests."""
    runtime = current_global_state(conn, cfg)
    depth = conn.execute(
        "SELECT count(*) FROM outbox WHERE state IN ('pending','retry')"
    ).fetchone()[0]
    delivered = 0
    error = None
    if cfg.mode == "active" and capability_allowed(conn, cfg, "outbound_any"):
        from .notification_digest import prepare

        prepare(conn, cfg)
        row = claim_outbox(
            conn,
            worker_id=f"outbox:{os.getpid()}",
            eligible=lambda value: outbox_eligible(conn, cfg, value),
        )
        if row:
            try:
                from .delivery import hermes_send, hermes_send_buttons

                delivery(
                    conn,
                    cfg,
                    row,
                    lark_runner=partial(
                        run_json, executable=cfg.runtime("lark_cli_command")
                    ),
                    mail_runner=partial(
                        run_mail_json, executable=cfg.runtime("lark_cli_command")
                    ),
                    telegram_runner=partial(
                        hermes_send, executable=cfg.runtime("hermes_command")
                    ),
                    telegram_button_runner=partial(
                        hermes_send_buttons,
                        executable=cfg.runtime("hermes_command"),
                    ),
                )
                delivered = 1
            except DeliverySuppressed:
                # Expected cooperative-preemption outcome, not transport failure.
                pass
            except Exception as exc:  # noqa: BLE001 - sender loop persists after classified delivery failure
                error = f"{type(exc).__name__}: {exc}"
    detail = {
        "outbox_depth": depth,
        "delivered": delivered,
        "delivery_enabled": cfg.mode == "active"
        and capability_allowed(conn, cfg, "outbound_any"),
        "runtime_mode": runtime["mode"],
        "error": error,
    }
    heartbeat(conn, "outbox", "ready" if error is None else "degraded", detail)
    return {"ready": error is None, "mode": cfg.mode, **detail}


def outbox_main() -> None:
    _run("outbox", _outbox_tick, 5)


def _attention_tick(conn, cfg):
    from .attention_subscriptions import collect

    if not cfg.feature("mail") or not capability_allowed(conn, cfg, "triage"):
        return {"created": 0, "external_messages_sent": 0, "skipped": True}
    return collect(conn, owner_id=cfg.control_operator_id, limit=100)


def _watch_tick(conn, cfg):
    from .db import transaction
    from .watch_subscriptions import collect_cases, collect_releases

    # Resolve lazy mode expiry before opening the collectors' transaction.
    if not capability_allowed(conn, cfg, "triage"):
        return {"created": 0, "external_messages_sent": 0, "skipped": True}
    # Mail's feature switch must not disable independently selected Case watches.
    with transaction(conn):
        releases = collect_releases(conn, owner_id=cfg.control_operator_id, limit=100)
        cases = collect_cases(conn, owner_id=cfg.control_operator_id, limit=100)
    return {"created": releases["created"] + cases["created"],
            "releases": releases["created"], "cases": cases["created"],
            "external_messages_sent": 0}


def reconcile_main() -> None:
    def tick(conn, cfg):
        result = reconcile(conn, config=cfg)
        from .base_sync import enqueue_dirty_entities
        from .db import transaction
        from .executors import BoardExecutor
        from .store import transition_case

        cleaned: list[str] = []
        cleanup_failures: list[dict[str, str]] = []
        cleanup_waiting: list[dict[str, str]] = []
        from .board_operation_guard import BoardOperationBusy
        expired = conn.execute(
            """SELECT owner,case_id,metadata_json FROM locks
               WHERE lock_key='board1' AND expires_at<?""",
            (iso_now(),),
        ).fetchall()
        for lock in expired:
            metadata = json.loads(lock["metadata_json"])
            session_id = metadata.get("session_id")
            if not isinstance(session_id, str):
                cleanup_failures.append(
                    {"case_id": str(lock["case_id"]), "error": "lock has no session_id"}
                )
                continue
            try:
                BoardExecutor(cfg).close_session(
                    conn,
                    case_id=lock["case_id"],
                    session_id=session_id,
                )
                cleaned.append(session_id)
                case = conn.execute(
                    "SELECT state,version FROM cases WHERE case_id=?",
                    (lock["case_id"],),
                ).fetchone()
                if case is not None and case["state"] == "board_testing":
                    transition_case(
                        conn,
                        case_id=lock["case_id"],
                        after="investigating",
                        actor_type="system",
                        actor_id="reconcile",
                        reason="expired board1 lease cleaned to BROM",
                        expected_version=case["version"],
                        idempotency_key=f"board-cleanup:{session_id}:expired",
                    )
            except BoardOperationBusy:
                # No hardware action was dispatched. Preserve the exact lease;
                # a later reconcile tick can retry without failure notifications.
                cleanup_waiting.append({"case_id": str(lock["case_id"]), "session_id": session_id})
            except Exception as exc:  # noqa: BLE001 - failed cleanup must alert and retain the lock
                error = f"{type(exc).__name__}: {exc}"[:1000]
                cleanup_failures.append(
                    {"case_id": str(lock["case_id"]), "error": error}
                )
                if notice_destination(cfg):
                    with transaction(conn):
                        enqueue_notice(
                            conn, cfg,
                            action_type="board_cleanup_failed",
                            payload={
                                "text": (
                                    "board1 自动收尾未验证\n"
                                    f"Case: {lock['case_id']}\nSession: {session_id}\n"
                                    f"错误: {error}\n已停止复用 board1，请人工接管。"
                                )
                            },
                            idempotency_key=f"board-cleanup:{session_id}:failed",
                            case_id=lock["case_id"],
                        )
        result["board_cleaned_sessions"] = cleaned
        result["board_cleanup_failures"] = cleanup_failures
        result["board_cleanup_waiting"] = cleanup_waiting
        result["base_sync"] = enqueue_dirty_entities(conn, cfg)
        result["attention"] = _attention_tick(conn, cfg)
        result["watches"] = _watch_tick(conn, cfg)
        ready = result["integrity"]["ok"] and not cleanup_failures
        heartbeat(
            conn,
            "reconcile",
            "ready" if ready else "failed",
            result,
        )
        return {"ready": ready, **result}

    _run("reconcile", tick, 60)
