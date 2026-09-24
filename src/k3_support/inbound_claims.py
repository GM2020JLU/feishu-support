"""Attempt-scoped inbound ownership and an internal stale-worker write fence.

This is an application correctness boundary, not a sandbox against same-UID
Python code. All processing helpers receive the restricted connection; external
model/contact calls run between its short SQLite transactions.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .db import connect, transaction
from .ids import canonical_json, new_id
from .timeutil import parse_iso


class InboundClaimLost(RuntimeError):
    def __init__(self, message: str, *, error_class: str | None = None):
        super().__init__(message)
        self.error_class = error_class


class InboundContextSuperseded(InboundClaimLost):
    """A newer persisted conversation input/owner superseded this attempt."""


def supersede_claim(conn, claim: InboundClaim) -> bool:
    """Terminal only for this exact attempt; never finish a successor's claim.

    This recovery write intentionally does not touch Case/Turn/Outbox/jobs. It
    is called on the raw connection after the guarded transaction rolled back.
    """
    with transaction(conn):
        return (
            conn.execute(
                """UPDATE inbound_events SET status='ignored',last_error='context_superseded',
               lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=NULL
               WHERE event_pk=? AND claim_token=? AND lease_owner=? AND status='claimed'""",
                (claim.event_pk, claim.token, claim.worker_id),
            ).rowcount
            == 1
        )


class InboundHandle(str):
    """String-compatible event ID carrying the exact reservation, not just PID."""

    def __new__(cls, event_pk: str, claim_token: str):
        obj = super().__new__(cls, event_pk)
        obj._claim_token = claim_token
        return obj

    @property
    def claim_token(self):
        return self._claim_token


@dataclass(frozen=True)
class InboundClaim:
    event_pk: str
    worker_id: str
    token: str
    control_revision: int | None


def _control_revision(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT mode,revision,auto_expires_at FROM global_control_state WHERE scope='feishu_support'"
    ).fetchone()
    if row and row["mode"] in {"paused", "stopped"}:
        raise InboundClaimLost("inbound processing is paused or stopped")
    if (
        row
        and row["mode"] == "auto_60"
        and (
            not row["auto_expires_at"]
            or parse_iso(row["auto_expires_at"]) <= datetime.now(UTC)
        )
    ):
        raise InboundClaimLost("inbound automatic window expired")
    return int(row["revision"]) if row else None


def _check_claim(
    conn: sqlite3.Connection, claim: InboundClaim, *, now: datetime | None = None
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM inbound_events WHERE event_pk=?", (claim.event_pk,)
    ).fetchone()
    if (
        row is None
        or row["status"] != "claimed"
        or row["claim_token"] != claim.token
        or row["lease_owner"] != claim.worker_id
        or not row["lease_expires_at"]
        or parse_iso(row["lease_expires_at"]) <= (now or datetime.now(UTC))
    ):
        raise InboundClaimLost("inbound attempt expired or was superseded")
    if _control_revision(conn) != claim.control_revision:
        raise InboundClaimLost("inbound control revision changed")
    return row


def publish_worker_health(
    conn: sqlite3.Connection,
    worker_id: str,
    status: str,
    detail: dict[str, Any],
    *,
    register: bool = False,
    expected_token: str | None = None,
    now: datetime | None = None,
) -> bool:
    """A dead/replaced process or an old renewal thread cannot claim health."""
    stamp = (now or datetime.now(UTC)).isoformat()
    payload = canonical_json({**detail, "worker_id": worker_id})
    if register:
        conn.execute(
            """INSERT INTO service_state(component,pid,started_at,heartbeat_at,status,detail_json)
               VALUES('worker',?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET
               pid=excluded.pid,started_at=excluded.started_at,heartbeat_at=excluded.heartbeat_at,
               status=excluded.status,detail_json=excluded.detail_json""",
            (os.getpid(), stamp, stamp, status, payload),
        )
        return True
    return (
        conn.execute(
            """UPDATE service_state SET heartbeat_at=?,status=?,detail_json=?
           WHERE component='worker' AND json_extract(detail_json,'$.worker_id')=?
             AND (? IS NULL OR json_extract(detail_json,'$.claim_token')=?)""",
            (stamp, status, payload, worker_id, expected_token, expected_token),
        ).rowcount
        == 1
    )


def renew_inbound_claim(
    conn: sqlite3.Connection,
    claim: InboundClaim,
    *,
    lease_seconds: float = 120,
    report_worker_health: bool = False,
    bind_worker: bool = False,
    now: datetime | None = None,
) -> None:
    observed = now or datetime.now(UTC)
    with transaction(conn):
        _check_claim(conn, claim, now=observed)
        if report_worker_health:
            service = conn.execute(
                "SELECT detail_json FROM service_state WHERE component='worker'"
            ).fetchone()
            detail = json.loads(service[0]) if service else {}
            if detail.get("worker_id") != claim.worker_id or (
                not bind_worker and detail.get("claim_token") != claim.token
            ):
                raise InboundClaimLost("superseded worker service instance or attempt")
        conn.execute(
            """UPDATE inbound_events SET heartbeat_at=?,lease_expires_at=?
               WHERE event_pk=? AND claim_token=? AND lease_owner=?""",
            (
                observed.isoformat(),
                (observed + timedelta(seconds=lease_seconds)).isoformat(),
                claim.event_pk,
                claim.token,
                claim.worker_id,
            ),
        )
        if report_worker_health:
            publish_worker_health(
                conn,
                claim.worker_id,
                "ready",
                {
                    "event_pk": claim.event_pk,
                    "claim_token": claim.token,
                    "heartbeat_phase": "running",
                },
                now=observed,
            )


def claim_events(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    limit: int = 1,
    lease_seconds: float = 120,
) -> list[InboundHandle]:
    now = datetime.now(UTC)
    expires = (now + timedelta(seconds=lease_seconds)).isoformat()
    with transaction(conn):
        _control_revision(conn)
        rows = conn.execute(
            """SELECT event_pk FROM inbound_events WHERE
               (status='new' AND (next_attempt_at IS NULL OR next_attempt_at<=?))
               OR (status='claimed' AND lease_expires_at<=?)
               ORDER BY received_epoch,received_at,rowid LIMIT ?""",
            (now.isoformat(), now.isoformat(), limit),
        ).fetchall()
        result = []
        for row in rows:
            token = new_id("icl")
            conn.execute(
                """UPDATE inbound_events SET status='claimed',lease_owner=?,
                   lease_expires_at=?,claim_token=?,processing_started_at=NULL,
                   heartbeat_at=?,attempt_count=attempt_count+1 WHERE event_pk=?""",
                (worker_id, expires, token, now.isoformat(), row[0]),
            )
            result.append(InboundHandle(str(row[0]), token))
        return result


def start_processing(
    conn: sqlite3.Connection,
    *,
    event_pk: str,
    worker_id: str,
    expected_token: str | None,
    lease_seconds: float,
) -> tuple[InboundClaim | None, sqlite3.Row | dict[str, Any]]:
    """Start a reservation exactly once; an ID/owner alone cannot adopt one."""
    with transaction(conn):
        event = conn.execute(
            "SELECT * FROM inbound_events WHERE event_pk=?", (str(event_pk),)
        ).fetchone()
        if event is None:
            raise LookupError(event_pk)
        if event["status"] == "processed":
            linked = conn.execute(
                "SELECT case_id FROM case_events WHERE source_event_pk=?",
                (str(event_pk),),
            ).fetchone()
            return None, {
                "processed": False,
                "reason": "duplicate",
                "case_id": linked[0] if linked else None,
            }
        if event["status"] not in {"new", "claimed"}:
            return None, {"processed": False, "reason": event["status"]}
        revision = _control_revision(conn)
        if event["status"] == "new" and expected_token is None:
            token = new_id("icl")
            now = datetime.now(UTC)
            conn.execute(
                """UPDATE inbound_events SET status='claimed',lease_owner=?,claim_token=?,
                   lease_expires_at=?,heartbeat_at=?,processing_started_at=NULL,
                   attempt_count=attempt_count+1 WHERE event_pk=?""",
                (
                    worker_id,
                    token,
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    str(event_pk),
                ),
            )
        elif expected_token is None:
            return None, {
                "processed": False,
                "reason": (
                    "claimed_by_other_worker"
                    if event["lease_owner"] != worker_id
                    else "claim_token_required"
                ),
                "lease_owner": event["lease_owner"],
            }
        else:
            token = expected_token
        claim = InboundClaim(str(event_pk), worker_id, token, revision)
        current = _check_claim(conn, claim)
        if current["processing_started_at"] is not None:
            raise InboundClaimLost("inbound attempt has already started")
        conn.execute(
            "UPDATE inbound_events SET processing_started_at=? WHERE event_pk=? AND claim_token=?",
            (datetime.now(UTC).isoformat(), str(event_pk), token),
        )
        return claim, current


def fail_claim(
    conn: sqlite3.Connection,
    claim: InboundClaim,
    exc: Exception,
    *,
    guarded: FencedConnection | None = None,
) -> bool:
    """A late failure may not reset or poison a successor (even same worker)."""
    with transaction(conn):
        try:
            row = guarded._check() if guarded is not None else _check_claim(conn, claim)
        except InboundClaimLost:
            return False
        attempts = int(row["attempt_count"])
        state = "dead_letter" if attempts >= 8 else "new"
        delay = min(300, 5 * (3 ** max(0, attempts - 1)))
        retry = (
            (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
            if state == "new"
            else None
        )
        conn.execute(
            """UPDATE inbound_events SET status=?,last_error=?,lease_owner=NULL,
               lease_expires_at=NULL,next_attempt_at=? WHERE event_pk=? AND claim_token=?""",
            (state, f"{type(exc).__name__}: {exc}", retry, claim.event_pk, claim.token),
        )
        return True


class InboundHeartbeat:
    """Renew with an independent connection; failure is sticky for this handle."""

    def __init__(
        self,
        database_path: Path | None,
        claim: InboundClaim,
        *,
        lease_seconds: float,
        interval_seconds: float,
        stop_requested: Callable[[], bool],
        report_worker_health: bool = False,
    ) -> None:
        self.failed = threading.Event()
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.stop_requested = stop_requested
        self.claim = claim
        self.error_class: str | None = None
        self.reason: str | None = None
        if database_path is None:
            # In-memory databases cannot be renewed through an independent
            # connection. The write fence still enforces their original expiry.
            return

        def fail(exc: Exception) -> None:
            self.reason = (
                str(exc) if isinstance(exc, InboundClaimLost) else "heartbeat_error"
            )
            self.error_class = (
                None if isinstance(exc, InboundClaimLost) else type(exc).__name__
            )
            self.failed.set()
            if report_worker_health and self.error_class is not None:
                # Best-effort signal on a fresh connection. No late publisher
                # may replace a different process or another attempt's status.
                error_conn = None
                try:
                    error_conn = connect(database_path)
                    publish_worker_health(
                        error_conn,
                        claim.worker_id,
                        "degraded",
                        {
                            "event_pk": claim.event_pk,
                            "claim_token": claim.token,
                            "heartbeat_phase": "heartbeat_error",
                            "error_class": self.error_class,
                        },
                        expected_token=claim.token,
                    )
                except Exception:  # noqa: BLE001, S110 - reporting failure must not mask the already-sticky lease failure
                    pass
                finally:
                    if error_conn is not None:
                        error_conn.close()

        if report_worker_health:
            initial = None
            try:
                initial = connect(database_path)
                renew_inbound_claim(
                    initial,
                    claim,
                    lease_seconds=lease_seconds,
                    report_worker_health=True,
                    bind_worker=True,
                )
            except Exception as exc:  # noqa: BLE001 - any heartbeat setup failure must fence the processing attempt
                fail(exc)
                return
            finally:
                if initial is not None:
                    initial.close()

        def renew() -> None:
            heartbeat_conn = None
            try:
                heartbeat_conn = connect(database_path)
                while not self.stopped.wait(min(interval_seconds, lease_seconds / 3)):
                    if stop_requested():
                        raise InboundClaimLost("worker stop requested")
                    renew_inbound_claim(
                        heartbeat_conn,
                        claim,
                        lease_seconds=lease_seconds,
                        report_worker_health=report_worker_health,
                    )
            except Exception as exc:  # noqa: BLE001 - uncaught thread failures must become a sticky fenced failure
                fail(exc)
            finally:
                if heartbeat_conn is not None:
                    heartbeat_conn.close()

        self.thread = threading.Thread(
            target=renew, name=f"inbound-{claim.token}", daemon=True
        )
        self.thread.start()

    def check(self) -> None:
        if self.failed.is_set():
            raise InboundClaimLost(
                self.reason or "inbound heartbeat lost", error_class=self.error_class
            )
        if self.stop_requested():
            raise InboundClaimLost("inbound heartbeat lost or worker stop requested")

    def close(self) -> None:
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=6)
            if self.thread.is_alive():
                self.failed.set()
                raise InboundClaimLost(
                    "heartbeat thread did not stop", error_class="HeartbeatStopTimeout"
                )


class FencedCursor:
    """No raw writable cursor/connection is leaked by the restricted API."""

    def __init__(self, connection: FencedConnection, cursor=None):
        self.connection = connection
        self._cursor = cursor

    def execute(self, sql, parameters=()):
        self._cursor = self.connection.execute(sql, parameters)._cursor
        return self

    def executemany(self, sql, parameters):
        self._cursor = self.connection.executemany(sql, parameters)._cursor
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        return (
            self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        )

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def description(self):
        return self._cursor.description

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._cursor)

    def close(self):
        if self._cursor is not None:
            self._cursor.close()


class FencedConnection:
    """SQLite write-lock/check/use boundary for every downstream DB operation.

    Even reads acquire short write locks outside explicit transactions. This
    avoids guessing whether WITH/PRAGMA/SQL functions mutate state. Unknown APIs
    are not forwarded. Network calls must never run inside a transaction.
    """

    def __init__(
        self, conn: sqlite3.Connection, claim: InboundClaim, heartbeat: InboundHeartbeat
    ):
        self._conn = conn
        self.claim = claim
        self._heartbeat = heartbeat
        self._terminal: tuple[str, datetime] | None = None
        self._savepoints: list[str] = []
        self._savepoint_root = False
        self._case_epochs: dict[str, int] = {}
        self._turn_fences: dict[str, int] = {}
        self._context_transition_depth = 0
        self._context_stamp = self._read_context_stamp()
        self._transaction_context_stamp = self._context_stamp
        self._savepoint_context_stamps: list[dict | None] = []

    @property
    def in_transaction(self):
        return self._conn.in_transaction

    @property
    def row_factory(self):
        return self._conn.row_factory

    def _check(self):
        self._heartbeat.check()
        if self._terminal is not None:
            raise InboundClaimLost("terminal inbound update must be the last operation")
        row = _check_claim(self._conn, self.claim)
        self._check_context()
        self._check_case_authority()
        return row

    def _read_context_stamp(self):
        from .conversation_context import event_context_fence

        stamp = event_context_fence(self._conn, self.claim.event_pk)
        # dirty -> ready is a projection of the same inputs, not a new input or
        # permission. All owner/control changes already advance the revision.
        return (
            {key: value for key, value in stamp.items() if key != "state"}
            if stamp
            else None
        )

    def _check_context(self):
        if (
            not self._context_transition_depth
            and self._read_context_stamp() != self._context_stamp
        ):
            raise InboundContextSuperseded(
                "inbound conversation context was superseded"
            )

    @contextmanager
    def context_transition(self):
        """Trusted internal Case association, under an existing SQLite write lock.

        Only conversation_context.binding_atomic calls this. External/model
        callbacks remain forbidden by checkpoint while this transaction is open.
        Claim, global, Case and Turn checks remain enabled throughout the scope.
        """
        if not self.in_transaction:
            raise RuntimeError(
                "context association requires an existing write transaction"
            )
        self._check()
        previous = self._context_stamp
        self._context_transition_depth += 1
        try:
            yield
            if self._context_transition_depth == 1:
                self._context_stamp = self._read_context_stamp()
        except BaseException:
            self._context_stamp = previous
            raise
        finally:
            self._context_transition_depth -= 1

    def _case_epoch(self, case_id: str) -> int:
        case = self._conn.execute(
            "SELECT state FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if case is None or case["state"] in {
            "paused",
            "takeover",
            "cancelled",
            "resolved",
        }:
            raise InboundClaimLost("inbound Case authority was withdrawn")
        return int(
            self._conn.execute(
                "SELECT coalesce(max(sequence),0) FROM case_events WHERE case_id=? AND actor_type='operator'",
                (case_id,),
            ).fetchone()[0]
        )

    def _check_case_authority(self) -> None:
        # Only exact persisted event associations, never a guessed chat match.
        associated = self._conn.execute(
            """SELECT case_id FROM case_events WHERE source_event_pk=?
               UNION SELECT case_id FROM route_decisions WHERE event_pk=? AND case_id IS NOT NULL
               UNION SELECT case_id FROM conversation_turns WHERE source_event_pk=?""",
            (self.claim.event_pk, self.claim.event_pk, self.claim.event_pk),
        ).fetchall()
        for row in associated:
            case_id = str(row[0])
            if case_id not in self._case_epochs:
                self._case_epochs[case_id] = self._case_epoch(case_id)
        for case_id, epoch in self._case_epochs.items():
            if self._case_epoch(case_id) != epoch:
                raise InboundClaimLost("operator changed inbound Case authority")
        turn = self._conn.execute(
            "SELECT turn_id,fence,communication_owner FROM conversation_turns WHERE source_event_pk=?",
            (self.claim.event_pk,),
        ).fetchone()
        if turn is not None:
            if turn["communication_owner"] != "ai":
                raise InboundClaimLost("operator claimed inbound communication")
            turn_id, fence = str(turn["turn_id"]), int(turn["fence"])
            if self._turn_fences.setdefault(turn_id, fence) != fence:
                raise InboundClaimLost("inbound conversation fence changed")

    def bind_case(self, case_id: str) -> None:
        # A selected continuation gains an exact association before ensure_turn
        # or attachment writes. Own Case version changes do not fence this work.
        with transaction(self):
            epoch = self._case_epoch(case_id)
            if self._case_epochs.setdefault(case_id, epoch) != epoch:
                raise InboundClaimLost("operator changed inbound Case authority")

    def checkpoint(self) -> None:
        if self.in_transaction:
            raise RuntimeError(
                "external inbound callback cannot run in a database transaction"
            )
        with transaction(self):
            pass

    def _commit_check(self):
        if self._terminal is None:
            self._check()
        else:
            self._heartbeat.check()
            if _control_revision(
                self._conn
            ) != self.claim.control_revision or self._terminal[1] <= datetime.now(UTC):
                raise InboundClaimLost("inbound authority lost before terminal commit")
            self._check_case_authority()
            self._check_context()

    def _rollback(self):
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")
        self._terminal = None
        self._savepoints.clear()
        self._savepoint_root = False
        self._context_stamp = self._transaction_context_stamp
        self._savepoint_context_stamps.clear()

    def execute(self, sql: str, parameters=()):
        # Transaction statements have deliberately narrow, explicit semantics.
        normalized = sql.strip().rstrip(";").strip()
        upper = normalized.upper()
        if upper in {"BEGIN", "BEGIN IMMEDIATE", "BEGIN DEFERRED", "BEGIN EXCLUSIVE"}:
            cursor = self._conn.execute("BEGIN IMMEDIATE")
            self._transaction_context_stamp = self._context_stamp
            try:
                self._check()
            except BaseException:
                self._rollback()
                raise
            return FencedCursor(self, cursor)
        if upper in {"COMMIT", "END"}:
            try:
                self._commit_check()
                cursor = self._conn.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
            self._savepoints.clear()
            self._savepoint_root = False
            self._savepoint_context_stamps.clear()
            return FencedCursor(self, cursor)
        if upper == "ROLLBACK":
            self._rollback()
            return FencedCursor(self)
        savepoint = re.fullmatch(
            r"(SAVEPOINT|RELEASE(?: SAVEPOINT)?|ROLLBACK TO(?: SAVEPOINT)?)\s+([A-Za-z_][A-Za-z0-9_]*)",
            upper,
        )
        if savepoint:
            operation, name = savepoint.groups()
            if operation == "SAVEPOINT":
                if not self.in_transaction:
                    self.execute("BEGIN IMMEDIATE")
                    self._savepoint_root = True
                self._check()
                cursor = self._conn.execute(normalized)
                self._savepoints.append(name)
                self._savepoint_context_stamps.append(self._context_stamp)
            else:
                if name not in self._savepoints:
                    raise sqlite3.OperationalError("unknown guarded savepoint")
                index = len(self._savepoints) - 1 - self._savepoints[::-1].index(name)
                if operation.startswith("ROLLBACK"):
                    cursor = self._conn.execute(normalized)
                    self._terminal = None
                    self._context_stamp = self._savepoint_context_stamps[index]
                    del self._savepoint_context_stamps[index + 1 :]
                    del self._savepoints[index + 1 :]
                else:
                    self._check()
                    cursor = self._conn.execute(normalized)
                    del self._savepoints[index:]
                    del self._savepoint_context_stamps[index:]
                    if not self._savepoints and self._savepoint_root:
                        self.execute("COMMIT")
            return FencedCursor(self, cursor)
        first = re.match(r"[A-Za-z]+", normalized)
        if first is None or first[0].upper() not in {
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "WITH",
            "EXPLAIN",
        }:
            raise sqlite3.ProgrammingError(
                "unsupported SQL in inbound processing connection"
            )
        standalone = not self.in_transaction
        if standalone:
            self.execute("BEGIN IMMEDIATE")
        try:
            self._check()
            cursor = self._conn.execute(sql, parameters)
            if standalone:
                self.execute("COMMIT")
            return FencedCursor(self, cursor)
        except BaseException:
            if standalone:
                self._rollback()
            raise

    def executemany(self, sql: str, parameters: Iterable):
        # One transaction; preserve SQLite's aggregate rowcount and DML-only
        # semantics while fencing every consumed parameter set.
        first = re.match(r"[A-Za-z]+", sql.strip())
        if first is None or first[0].upper() not in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "WITH",
        }:
            raise sqlite3.ProgrammingError("executemany requires inbound DML")
        standalone = not self.in_transaction
        if standalone:
            self.execute("BEGIN IMMEDIATE")

        def guarded_parameters():
            for values in parameters:
                self._check()
                yield values

        try:
            self._check()
            cursor = self._conn.executemany(sql, guarded_parameters())
            if standalone:
                self.execute("COMMIT")
            return FencedCursor(self, cursor)
        except BaseException:
            if standalone:
                self._rollback()
            raise

    def cursor(self):
        return FencedCursor(self)

    def commit(self):
        return self.execute("COMMIT")

    def rollback(self):
        return self.execute("ROLLBACK")

    def __enter__(self):
        self.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.rollback() if exc_type is not None else self.commit()

    def finish(self, status: str) -> None:
        if status not in {"processed", "ignored"} or not self.in_transaction:
            raise ValueError("inbound finish requires a final terminal transaction")
        row = self._check()
        self._conn.execute(
            """UPDATE inbound_events SET status=?,lease_owner=NULL,lease_expires_at=NULL
               WHERE event_pk=? AND claim_token=? AND lease_owner=? AND status='claimed'""",
            (status, self.claim.event_pk, self.claim.token, self.claim.worker_id),
        )
        self._terminal = (status, parse_iso(row["lease_expires_at"]))
