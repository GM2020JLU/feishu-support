"""Durable GUI dispatch, isolated from HTTP; never reclaim uncertain work."""

from __future__ import annotations

from .calendar import execute_meeting_create
from .db import transaction
from .ids import canonical_json
from .runtime_control import capability_allowed
from .timeutil import iso_now


def dispatch_one(conn, config, *, runner):
    if (
        config.mode != "active"
        or not config.feature("calendar")
        or not capability_allowed(conn, config, "calendar")
    ):
        return None
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM meeting_dispatch_queue WHERE state='queued' ORDER BY created_at,preview_id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE meeting_dispatch_queue SET state='dispatched',updated_at=? WHERE preview_id=? AND state='queued'",
            (iso_now(), row["preview_id"]),
        )
    # A crashed dispatched item is never reclaimed: the exact attempt ledger
    # owns side-effect recovery, not a timeout-based retry.
    try:
        result = execute_meeting_create(
            conn, config, preview_id=row["preview_id"], runner=runner
        )
        state = (
            "finished"
            if result.get("outcome") in {"complete", "linked"}
            else "needs_review"
        )
    except Exception as exc:  # noqa: BLE001 - adapter failures must remain non-retryable
        state = "needs_review"
        result = {
            "error_type": type(exc).__name__,
            "automatic_retry": False,
            "message": "创建未完整确认，请核对原会议记录；不要重复创建。",
        }
    with transaction(conn):
        conn.execute(
            "UPDATE meeting_dispatch_queue SET state=?,updated_at=?,result_json=? WHERE preview_id=? AND state='dispatched'",
            (state, iso_now(), canonical_json(result), row["preview_id"]),
        )
    return {"preview_id": row["preview_id"], "state": state, "result": result}
