"""Queued read-only settlement of an operator's dispatched field/transition write.

Mirrors the comment reconciliation path: acknowledged-but-unconfirmed writes
(lost acknowledgements, snapshot or op-record indexing lag) get re-checked by a
scoped read against Project instead of requiring hand-written human evidence.
Observing old effects never reauthorizes anything; the transport is built over
a read-only client and this path never calls dispatch or write.
"""

import json

from . import project_bug_operations as operations
from .project_comment_reconcile import reader_identity
from .project_write_transport import WRITE_ACTIONS, WriteTransport
from .timeutil import iso_now


def selection(conn, actor, source):
    if not isinstance(source, dict) or set(source) != {"operation_id", "write_digest"}:
        raise ValueError("exact write operation identity required")
    op = operations._operation(conn, source["operation_id"])
    if (
        op["actor"] != actor
        or op["action"] not in WRITE_ACTIONS
        or not isinstance(source["write_digest"], str)
        or op["write_digest"] != source["write_digest"]
        or not op["write_json"]
    ):
        raise ValueError("owned dispatched write required")
    return op


def enqueue(conn, config, *, actor, operation_id, write_digest, grant_id, request_id):
    from .project_activity import enqueue as queue

    source = {"operation_id": operation_id, "write_digest": write_digest}
    op = selection(conn, actor, source)
    return queue(
        conn,
        config,
        actor=actor,
        bug_id=op["bug_id"],
        grant_id=grant_id,
        request_id=request_id,
        kind="write_reconcile",
        source=source,
    )


def collect(conn, client, reader, *, actor, source, guard):
    op = selection(conn, actor, source)
    # Intentionally use MeegleReadClient, which has no update/transition
    # methods. Contract flags are false and this path never calls write();
    # reconcile only performs scoped snapshot and op-record reads.
    transport = WriteTransport(
        conn,
        client,
        reader_digest=reader_identity(reader),
        before_read=guard,
        allowed_fields=frozenset(),
        risk_policy="recheck_window",
        closing_status_ids=frozenset(),
        update_contract_verified=False,
        transition_contract_verified=False,
    )
    guard()
    result = operations.reconcile(
        conn, operation_id=op["operation_id"], transport=transport, before_settle=guard
    )
    attempt = conn.execute(
        "SELECT state FROM project_write_attempts WHERE operation_id=?",
        (op["operation_id"],),
    ).fetchone()
    return {
        "items": [
            {
                "operation_id": op["operation_id"],
                "state": result["state"],
                "result": json.loads(result["result_json"])
                if result["result_json"]
                else None,
                "receipt_state": attempt["state"] if attempt else "missing",
            }
        ],
        "observed_at": iso_now(),
        "end_time_ms": None,
        "write_performed": False,
    }
