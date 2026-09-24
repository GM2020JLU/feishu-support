"""Observed reopen boundaries invalidate old-round closure evidence, not history.

Use immutable local event order, not millisecond timestamps. We cannot infer a
remote close/reopen cycle that happened entirely between our observations.
"""

from .project_bugs import BugConflict


def inspect(conn, *, bug_id, round_id, closing_status_ids=()):
    """Project known lifecycle boundaries without rewriting historical results.

    ``current`` means no boundary was found in local observations; it does not
    prove the absence of an unobserved remote close/reopen cycle.
    """
    def result(state, reason):
        return {"state": state, "reason": reason,
                "requires_new_round": None if state == "unavailable" else state != "current",
                "source": "local_observed_lifecycle"}

    start = conn.execute(
        "SELECT rowid FROM project_bug_events WHERE bug_id=? AND round_id=? "
        "AND kind='round_started' ORDER BY rowid LIMIT 1", (bug_id, round_id),
    ).fetchone()
    if start is None:
        return result("unavailable", "investigation lifecycle evidence is unavailable")
    boundary = start[0]
    if conn.execute(
        "SELECT 1 FROM project_bug_events e JOIN project_bug_operations o "
        "ON o.operation_id=json_extract(e.detail_json,'$.operation_id') "
        "WHERE e.bug_id=? AND o.bug_id=e.bug_id AND e.rowid>? "
        "AND e.kind IN ('write_observed','write_settled_by_human') "
        "AND o.action='bug.close' AND o.state='confirmed' LIMIT 1", (bug_id, boundary),
    ).fetchone():
        return result("closed_once", "this investigation already closed the Bug; start a new round and verify again")

    # Exact historical close receipts also classify their own Bug's terminal IDs;
    # additional IDs come only from trusted server configuration, never a label.
    terminal = set(closing_status_ids)
    terminal.update(row[0] for row in conn.execute(
        "SELECT json_extract(change_json,'$.target_status_id') FROM project_bug_operations "
        "WHERE bug_id=? AND action='bug.close' AND state='confirmed'", (bug_id,),
    ) if isinstance(row[0], str))

    def closed(row):
        if row[1] in terminal:
            return True
        explicit = row[2]
        if explicit in (0, 1):
            return bool(explicit)
        return row[1] in terminal if terminal else None

    query = (
        "SELECT e.rowid,json_extract(s.payload_json,'$.status_id'),"
        "json_extract(s.payload_json,'$.closure.closed') "
        "FROM project_bug_events e JOIN project_bug_snapshots s "
        "ON s.snapshot_id=json_extract(e.detail_json,'$.snapshot_id') "
        "WHERE e.bug_id=? AND s.bug_id=e.bug_id AND e.kind='observed' "
    )
    previous = conn.execute(query + "AND e.rowid<? ORDER BY e.rowid DESC LIMIT 1",
                            (bug_id, boundary)).fetchone()
    was_closed = closed(previous) if previous is not None else None
    for row in conn.execute(query + "AND e.rowid>? ORDER BY e.rowid", (bug_id, boundary)):
        now_closed = closed(row)
        if was_closed is True and now_closed is False:
            return result("reopened", "Bug reopened after this investigation began; start a new round and verify again")
        if now_closed is not None:
            was_closed = now_closed
    return result("current", "no close or reopen boundary found in local observations")


def require_current(conn, *, bug_id, round_id, closing_status_ids=()):
    state = inspect(conn, bug_id=bug_id, round_id=round_id,
                    closing_status_ids=closing_status_ids)
    if state["state"] != "current":
        raise BugConflict(state["reason"])
