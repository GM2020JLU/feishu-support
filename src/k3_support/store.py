from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .db import atomic, transaction
from .ids import canonical_json, inbound_key, new_id
from .state_machine import require_transition
from .timeutil import epoch_now, iso_now, parse_iso


class ConflictError(RuntimeError):
    pass


class NotFoundError(LookupError):
    pass


def _row(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> sqlite3.Row:
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise NotFoundError(args[0] if args else "record")
    return row


def ingest_event(
    conn: sqlite3.Connection,
    *,
    source: str,
    identity: str,
    external_id: str,
    payload: dict[str, Any],
    occurred_at: str,
    sender_id: str | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    raw_artifact_path: str | None = None,
    _context_hook: bool = True,
) -> tuple[str, bool]:
    from .conversation_context import atomic, record_known_ingress

    context_item = {
        "source": source,
        "identity": identity,
        "external_id": external_id,
        "payload": payload,
        "occurred_at": occurred_at,
        "sender_id": sender_id,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }

    def result(key, created):
        if _context_hook:
            record_known_ingress(conn, key, context_item)
        return key, created

    occurred = parse_iso(occurred_at)
    key = inbound_key(source, identity, external_id)
    event_pk = new_id("in")
    now = iso_now()
    with atomic(conn):
        # Keep replay compatibility with rows written before Feishu's two IM
        # transports shared one idempotency key. The shared key below closes
        # the concurrent race; this lookup closes sequential legacy replays.
        if source in {"feishu_bot_im", "feishu_user_poll"}:
            existing = conn.execute(
                """SELECT event_pk FROM inbound_events
                     WHERE external_id=?
                       AND source IN ('feishu_bot_im','feishu_user_poll')
                     ORDER BY received_epoch,event_pk LIMIT 1""",
                (external_id,),
            ).fetchone()
            if existing is not None:
                return result(str(existing[0]), False)
        cursor = conn.execute(
            """INSERT OR IGNORE INTO inbound_events(
                 event_pk,source,identity,external_id,idempotency_key,sender_id,chat_id,thread_id,
                 occurred_at,occurred_epoch,received_at,received_epoch,payload_json,raw_artifact_path)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_pk,
                source,
                identity,
                external_id,
                key,
                sender_id,
                chat_id,
                thread_id,
                occurred.isoformat(),
                int(occurred.timestamp()),
                now,
                epoch_now(),
                canonical_json(payload),
                raw_artifact_path,
            ),
        )
        if cursor.rowcount == 0:
            existing = conn.execute(
                "SELECT event_pk FROM inbound_events WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is None:
                existing = conn.execute(
                    "SELECT event_pk FROM inbound_events WHERE source=? AND identity=? AND external_id=?",
                    (source, identity, external_id),
                ).fetchone()
            if existing is None:
                raise ConflictError("event uniqueness conflict could not be resolved")
            return result(str(existing[0]), False)
        return result(event_pk, True)


def _next_case_id(conn: sqlite3.Connection, now: datetime) -> str:
    prefix = f"K3-{now.astimezone(UTC).strftime('%Y%m%d')}-"
    row = conn.execute(
        "SELECT case_id FROM cases WHERE case_id LIKE ? ORDER BY case_id DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    sequence = int(row[0].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}{sequence:04d}"


def create_case(
    conn: sqlite3.Connection,
    *,
    title: str,
    case_type: str,
    severity: str,
    confidence: float,
    requester_id: str | None = None,
    requester_chat_id: str | None = None,
    disclosure_class: str = "internal",
    source_event_pk: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[str, bool]:
    now_dt = datetime.now(UTC)
    now = now_dt.isoformat(timespec="milliseconds")
    with atomic(conn):
        if idempotency_key:
            existing = conn.execute(
                "SELECT case_id FROM case_events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return str(existing[0]), False
        case_id = _next_case_id(conn, now_dt)
        conn.execute(
            """INSERT INTO cases(case_id,title,type,severity,confidence,state,requester_id,
                   requester_chat_id,disclosure_class,created_at,created_epoch,updated_at,updated_epoch)
               VALUES(?,?,?,?,?,'intake',?,?,?,?,?,?,?)""",
            (
                case_id,
                title,
                case_type,
                severity,
                confidence,
                requester_id,
                requester_chat_id,
                disclosure_class,
                now,
                int(now_dt.timestamp()),
                now,
                int(now_dt.timestamp()),
            ),
        )
        conn.execute(
            """INSERT INTO case_events(event_id,case_id,sequence,event_type,actor_type,source_event_pk,
                   before_state,after_state,detail_json,idempotency_key,created_at,created_epoch)
               VALUES(?,?,1,'case_created','system',?,NULL,'intake','{}',?,?,?)""",
            (
                new_id("cev"),
                case_id,
                source_event_pk,
                idempotency_key,
                now,
                int(now_dt.timestamp()),
            ),
        )
    return case_id, True


def get_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    case = dict(_row(conn, "SELECT * FROM cases WHERE case_id=?", (case_id,)))
    case["events"] = [
        {**dict(row), "detail": json.loads(row["detail_json"])}
        for row in conn.execute(
            "SELECT * FROM case_events WHERE case_id=? ORDER BY sequence DESC LIMIT 20",
            (case_id,),
        )
    ]
    case["approvals"] = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM approvals WHERE case_id=? ORDER BY created_at DESC",
            (case_id,),
        )
    ]
    # Migration 011 is always present for current runtimes. Keep the control
    # receipt self-contained so an operator can see who owns communication.
    case["turns"] = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM conversation_turns WHERE case_id=? ORDER BY created_at DESC LIMIT 10",
            (case_id,),
        )
    ]
    return case


def transition_case(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    after: str,
    actor_type: str,
    actor_id: str | None,
    reason: str,
    expected_version: int,
    idempotency_key: str | None = None,
) -> int:
    with atomic(conn):
        if idempotency_key:
            previous = conn.execute(
                "SELECT ce.sequence,c.version FROM case_events ce JOIN cases c USING(case_id) "
                "WHERE ce.idempotency_key=? AND ce.case_id=?",
                (idempotency_key, case_id),
            ).fetchone()
            if previous:
                return int(previous["version"])
        row = _row(conn, "SELECT state,version FROM cases WHERE case_id=?", (case_id,))
        before, version = str(row["state"]), int(row["version"])
        if version != expected_version:
            raise ConflictError(
                f"stale case version: expected {expected_version}, current {version}"
            )
        require_transition(before, after)
        now = iso_now()
        owner = "operator" if after == "takeover" else None
        update = conn.execute(
            "UPDATE cases SET state=?,version=version+1,updated_at=?,updated_epoch=?,"
            "owner=coalesce(?,owner) WHERE case_id=? AND version=?",
            (after, now, epoch_now(), owner, case_id, expected_version),
        )
        if update.rowcount != 1:
            raise ConflictError("case changed during transition")
        sequence = conn.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO case_events(event_id,case_id,sequence,event_type,actor_type,actor_id,
                   before_state,after_state,detail_json,idempotency_key,created_at,created_epoch)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("cev"),
                case_id,
                sequence,
                "state_transition",
                actor_type,
                actor_id,
                before,
                after,
                canonical_json({"reason": reason}),
                idempotency_key,
                now,
                epoch_now(),
            ),
        )
        return expected_version + 1


def merge_case(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    canonical_case_id: str,
    actor_type: str,
    actor_id: str | None,
    reason: str,
    expected_version: int,
    idempotency_key: str,
) -> int:
    """Cancel a duplicate Case while preserving and linking all source events."""
    if case_id == canonical_case_id:
        raise ValueError("a Case cannot be merged into itself")
    now = iso_now()
    current_epoch = epoch_now()
    with transaction(conn):
        previous = conn.execute(
            "SELECT c.version FROM case_events ce JOIN cases c USING(case_id) "
            "WHERE ce.idempotency_key=? AND ce.case_id=?",
            (idempotency_key, case_id),
        ).fetchone()
        if previous:
            return int(previous["version"])
        source = _row(
            conn,
            "SELECT state,version,canonical_case_id FROM cases WHERE case_id=?",
            (case_id,),
        )
        target = _row(
            conn,
            "SELECT state,canonical_case_id FROM cases WHERE case_id=?",
            (canonical_case_id,),
        )
        if int(source["version"]) != expected_version:
            raise ConflictError(
                f"stale case version: expected {expected_version}, current {source['version']}"
            )
        if source["canonical_case_id"] is not None:
            raise ConflictError("source Case is already linked to a canonical Case")
        if target["canonical_case_id"] is not None:
            raise ValueError("target must be a root canonical Case")
        if target["state"] == "cancelled":
            raise ValueError("target canonical Case is cancelled")
        require_transition(str(source["state"]), "cancelled")
        active_job = conn.execute(
            """SELECT job_id FROM jobs WHERE case_id=?
               AND state IN ('queued','running','waiting') LIMIT 1""",
            (case_id,),
        ).fetchone()
        if active_job:
            raise ConflictError(f"source Case has active job {active_job['job_id']}")
        active_approval = conn.execute(
            """SELECT approval_id FROM approvals WHERE case_id=?
               AND status IN ('requested','approved') AND consumed_at IS NULL
               AND expires_at>? LIMIT 1""",
            (case_id, now),
        ).fetchone()
        if active_approval:
            raise ConflictError(
                f"source Case has active approval {active_approval['approval_id']}"
            )
        active_lock = conn.execute(
            "SELECT lock_key FROM locks WHERE case_id=? AND expires_at>? LIMIT 1",
            (case_id, now),
        ).fetchone()
        if active_lock:
            raise ConflictError(
                f"source Case has active lock {active_lock['lock_key']}"
            )

        source_events = conn.execute(
            """SELECT DISTINCT source_event_pk FROM case_events
               WHERE case_id=? AND source_event_pk IS NOT NULL""",
            (case_id,),
        ).fetchall()
        target_state = str(target["state"])
        for event in source_events:
            source_event_pk = str(event["source_event_pk"])
            link_key = f"merge:{case_id}:{canonical_case_id}:source:{source_event_pk}"
            if conn.execute(
                "SELECT 1 FROM case_events WHERE idempotency_key=?", (link_key,)
            ).fetchone():
                continue
            target_sequence = conn.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?",
                (canonical_case_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO case_events(event_id,case_id,sequence,event_type,
                       actor_type,actor_id,source_event_pk,before_state,after_state,
                       detail_json,idempotency_key,created_at,created_epoch)
                   VALUES(?,?,?,'duplicate_source_linked',?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("cev"),
                    canonical_case_id,
                    target_sequence,
                    actor_type,
                    actor_id,
                    source_event_pk,
                    target_state,
                    target_state,
                    canonical_json({"duplicate_case_id": case_id, "reason": reason}),
                    link_key,
                    now,
                    current_epoch,
                ),
            )

        changed = conn.execute(
            """UPDATE cases SET state='cancelled',canonical_case_id=?,version=version+1,
                   updated_at=?,updated_epoch=? WHERE case_id=? AND version=?""",
            (
                canonical_case_id,
                now,
                current_epoch,
                case_id,
                expected_version,
            ),
        )
        if changed.rowcount != 1:
            raise ConflictError("case changed during merge")
        source_sequence = conn.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO case_events(event_id,case_id,sequence,event_type,
                   actor_type,actor_id,before_state,after_state,detail_json,
                   idempotency_key,created_at,created_epoch)
               VALUES(?,?,?,'state_transition',?,?,?,'cancelled',?,?,?,?)""",
            (
                new_id("cev"),
                case_id,
                source_sequence,
                actor_type,
                actor_id,
                source["state"],
                canonical_json(
                    {"reason": reason, "canonical_case_id": canonical_case_id}
                ),
                idempotency_key,
                now,
                current_epoch,
            ),
        )
        conn.execute(
            "UPDATE cases SET updated_at=?,updated_epoch=? WHERE case_id=?",
            (now, current_epoch, canonical_case_id),
        )
        return expected_version + 1


def enqueue_outbox(
    conn: sqlite3.Connection,
    *,
    channel: str,
    action_type: str,
    destination: str,
    payload: dict[str, Any],
    idempotency_key: str,
    case_id: str | None = None,
    source_event_pk: str | None = None,
    turn_id: str | None = None,
    turn_revision: int | None = None,
    communication_fence: int | None = None,
    not_before: str | None = None,
    context_id: str | None = None,
    context_revision: int | None = None,
    context_digest: str | None = None,
) -> tuple[str, bool]:
    now = iso_now()
    outbox_id = new_id("out")
    runtime = conn.execute(
        "SELECT outbound_fence FROM global_control_state WHERE scope='feishu_support'"
    ).fetchone()
    global_outbound_fence = int(runtime[0]) if runtime is not None else 1
    cursor = conn.execute(
        """INSERT OR IGNORE INTO outbox(outbox_id,channel,action_type,destination,payload_json,
               idempotency_key,case_id,source_event_pk,turn_id,turn_revision,
               communication_fence,not_before,global_outbound_fence,created_at,updated_at,
               context_id,context_revision,context_digest)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            outbox_id,
            channel,
            action_type,
            destination,
            canonical_json(payload),
            idempotency_key,
            case_id,
            source_event_pk,
            turn_id,
            turn_revision,
            communication_fence,
            not_before,
            global_outbound_fence,
            now,
            now,
            context_id,
            context_revision,
            context_digest,
        ),
    )
    if cursor.rowcount == 0:
        existing = _row(
            conn,
            "SELECT outbox_id FROM outbox WHERE idempotency_key=?",
            (idempotency_key,),
        )
        return str(existing[0]), False
    return outbox_id, True


EXECUTABLE_CASE_STATES = (
    "triage",
    "investigating",
    "waiting_board",
    "board_testing",
    "waiting_push",
    "monitoring",
)


def claim_jobs(
    conn: sqlite3.Connection,
    worker_id: str,
    *,
    limit: int = 1,
    lease_seconds: int = 120,
    job_types: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    if job_types is not None and (
        not job_types
        or len(job_types) != len(set(job_types))
        or any(not isinstance(value, str) or not value for value in job_types)
    ):
        raise ValueError("job_types must be a non-empty unique tuple")
    from .timeutil import utc_now
    now = utc_now()
    expires = datetime.fromtimestamp(now.timestamp() + lease_seconds, UTC).isoformat()
    claimed: list[dict[str, Any]] = []
    with transaction(conn):
        placeholders = ",".join("?" for _ in EXECUTABLE_CASE_STATES)
        type_clause = ""
        type_args: tuple[str, ...] = ()
        if job_types is not None:
            type_clause = (
                " AND j.job_type IN (" + ",".join("?" for _ in job_types) + ")"
            )
            type_args = job_types
        rows = conn.execute(
            f"""SELECT j.job_id FROM jobs j LEFT JOIN cases c USING(case_id)
                WHERE j.state='queued' AND j.available_at<=?
                AND (j.job_type!='codex' OR json_type(j.context_json,'$.execution') IS NULL)
                AND (j.job_type='base_sync' OR c.state IN ({placeholders}))
                {type_clause}
                ORDER BY j.priority,j.created_at LIMIT ?""",
            (now.isoformat(timespec="microseconds"), *EXECUTABLE_CASE_STATES, *type_args, limit),
        ).fetchall()
        for row in rows:
            updated = conn.execute(
                "UPDATE jobs SET state='running',lease_owner=?,lease_expires_at=?,heartbeat_at=?,"
                "attempt_no=attempt_no+1,updated_at=? WHERE job_id=? AND state='queued'",
                (worker_id, expires, now.isoformat(), now.isoformat(), row[0]),
            )
            if updated.rowcount:
                claimed.append(
                    dict(_row(conn, "SELECT * FROM jobs WHERE job_id=?", (row[0],)))
                )
    return claimed


def renew_job_lease(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    worker_id: str,
    lease_seconds: int = 120,
    now: datetime | None = None,
) -> bool:
    """Extend one running job lease only while it is still owned by this worker."""
    observed = now or datetime.now(UTC)
    expires = datetime.fromtimestamp(
        observed.timestamp() + lease_seconds, UTC
    ).isoformat()
    with transaction(conn):
        changed = conn.execute(
            """UPDATE jobs SET heartbeat_at=?,lease_expires_at=?,updated_at=?
               WHERE job_id=? AND state='running' AND lease_owner=?""",
            (
                observed.isoformat(),
                expires,
                observed.isoformat(),
                job_id,
                worker_id,
            ),
        )
    return changed.rowcount == 1


def reclaim_stale_jobs(conn: sqlite3.Connection, now: str | None = None) -> list[str]:
    cutoff = now or iso_now()
    with transaction(conn):
        ids = [
            str(row[0])
            for row in conn.execute(
                "SELECT job_id FROM jobs WHERE state='running' AND lease_expires_at<?",
                (cutoff,),
            )
        ]
        if ids:
            conn.executemany(
                "UPDATE jobs SET state='orphaned',lease_owner=NULL,updated_at=?,error_class='stale_lease' WHERE job_id=?",
                [(cutoff, job_id) for job_id in ids],
            )
        return ids


def recover_stale_jobs(
    conn: sqlite3.Connection, now: str | None = None
) -> dict[str, list[str]]:
    """Retry only crash-safe jobs; orphan every potentially side-effecting job."""
    cutoff = now or iso_now()
    recovered: list[str] = []
    orphaned: list[str] = []
    with transaction(conn):
        rows = conn.execute(
            """SELECT j.job_id,j.job_type,j.attempt_no,j.max_attempts,j.case_id,c.state AS case_state
                 FROM jobs j LEFT JOIN cases c USING(case_id)
                WHERE j.state='running' AND j.lease_expires_at<?
                ORDER BY j.job_id""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            crash_safe = row["job_type"] in {"retrieve", "base_sync"}
            case_allows_retry = row["job_type"] == "base_sync" or (
                row["case_id"] is not None
                and row["case_state"] in EXECUTABLE_CASE_STATES
            )
            can_retry = (
                crash_safe
                and case_allows_retry
                and int(row["attempt_no"]) < int(row["max_attempts"])
            )
            state = "queued" if can_retry else "orphaned"
            error_class = "stale_lease_retry" if can_retry else "stale_lease"
            conn.execute(
                """UPDATE jobs SET state=?,available_at=?,lease_owner=NULL,
                       lease_expires_at=NULL,pid=NULL,process_start_token=NULL,
                       error_class=?,updated_at=?
                     WHERE job_id=? AND state='running'""",
                (state, cutoff, error_class, cutoff, row["job_id"]),
            )
            conn.execute(
                """UPDATE job_attempts SET ended_at=coalesce(ended_at,?),
                       result=coalesce(result,'interrupted'),detail_json=?
                     WHERE job_id=? AND attempt_no=?""",
                (
                    cutoff,
                    canonical_json(
                        {
                            "reason": "stale_lease",
                            "requeued": can_retry,
                        }
                    ),
                    row["job_id"],
                    row["attempt_no"],
                ),
            )
            (recovered if can_retry else orphaned).append(str(row["job_id"]))
    return {"recovered": recovered, "orphaned": orphaned}
