"""Queued read-only settlement of an operator's immutable comment attempt."""

import json

from . import project_bug_operations as operations
from .ids import digest
from .project_comment_transport import CommentTransport
from .timeutil import iso_now


def reader_identity(reader):
    # Stable across runtime mode changes and write-grant revocation; queued reads
    # separately fence runtime configuration and require a current read grant.
    return digest({"comment_reader": reader, "version": 1})


def selection(conn, actor, source):
    if not isinstance(source, dict) or set(source) != {"operation_id", "write_digest"}:
        raise ValueError("exact comment operation identity required")
    op = operations._operation(conn, source["operation_id"])
    if (
        op["actor"] != actor
        or op["action"] != "bug.comment"
        or not isinstance(source["write_digest"], str)
        or op["write_digest"] != source["write_digest"]
        or not op["write_json"]
    ):
        raise ValueError("owned dispatched comment required")
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
        kind="comment_reconcile",
        source=source,
    )


def collect(conn, client, reader, *, actor, source, guard):
    op = selection(conn, actor, source)
    # Intentionally use MeegleReadClient, which has no create_comment method.
    # Contract verification is false and this path never calls dispatch/write.
    transport = CommentTransport(
        conn, client, reader_digest=reader_identity(reader), before_read=guard
    )
    guard()
    result = operations.reconcile(
        conn, operation_id=op["operation_id"], transport=transport, before_settle=guard
    )
    attempt = conn.execute(
        "SELECT state FROM project_comment_attempts WHERE operation_id=?",
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
