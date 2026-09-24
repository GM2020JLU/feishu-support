"""Trusted authenticated-operator control for settling unknown Project writes.

This is not a worker RPC and does not reauthorize anything. A human operator
may attach explicit evidence to a previously attempted read-only reconciliation
result, but the settlement itself never clears uncertainty without recorded
human evidence and never grants any new authority.
"""

from .db import transaction
from .ids import canonical_json, digest, new_id
from .project_bug_operations import _operation
from .project_bugs import BugConflict, _event, _text
from .timeutil import iso_now


def _result_payload(settlement_id, verdict, evidence_digest):
    return canonical_json(
        {
            "outcome": "applied" if verdict == "confirmed_applied" else "rejected",
            "settled_by_human": True,
            "settlement_id": settlement_id,
            "evidence_digest": evidence_digest,
        }
    )


def projection(conn_or_row, operation_id=None):
    if operation_id is None:
        row = conn_or_row
    else:
        row = conn_or_row.execute(
            "SELECT * FROM project_unknown_settlements WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    if row is None:
        raise ValueError("unknown settlement does not exist")
    return {
        "settlement_id": row["settlement_id"],
        "operation_id": row["operation_id"],
        "verdict": row["verdict"],
        "evidence_digest": row["evidence_digest"],
        "created_at": row["created_at"],
    }


def detail(conn, *, operation_id, actor):
    _text(operation_id, "operation ID")
    _text(actor, "actor")
    op = _operation(conn, operation_id)
    if op["actor"] != actor:
        raise PermissionError("settlement requires the operation owner")
    row = conn.execute(
        "SELECT * FROM project_unknown_settlements WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown settlement does not exist")
    result = projection(row)
    result["actor"] = row["actor"]
    result["evidence_text"] = row["evidence_text"]
    return result


def settle(conn, *, operation_id, actor, verdict, evidence_text):
    _text(operation_id, "operation ID")
    _text(actor, "actor")
    _text(evidence_text, "settlement evidence", 10000)
    if verdict not in {"confirmed_applied", "confirmed_not_applied"}:
        raise ValueError("invalid verdict")
    with transaction(conn):
        op = _operation(conn, operation_id)
        if op["actor"] != actor:
            raise PermissionError("settlement requires the operation owner")
        existing = conn.execute(
            "SELECT * FROM project_unknown_settlements WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        evidence_digest = digest(evidence_text)
        if existing is not None:
            if (
                existing["verdict"] == verdict
                and existing["evidence_digest"] == evidence_digest
            ):
                return projection(existing)
            raise BugConflict(
                "operation was already settled with different evidence"
            )
        if op["state"] != "unknown":
            raise BugConflict("only unknown outcomes can be settled by a human")
        if conn.execute(
            "SELECT 1 FROM project_bug_operation_observations WHERE operation_id=? LIMIT 1",
            (operation_id,),
        ).fetchone() is None:
            raise BugConflict(
                "read-only reconciliation must be attempted before human settlement"
            )
        settlement_id = new_id("pus")
        result_json = _result_payload(settlement_id, verdict, evidence_digest)
        conn.execute(
            """INSERT INTO project_unknown_settlements
            (settlement_id,operation_id,actor,verdict,evidence_text,evidence_digest,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (
                settlement_id,
                operation_id,
                actor,
                verdict,
                evidence_text,
                evidence_digest,
                iso_now(),
            ),
        )
        state = "confirmed" if verdict == "confirmed_applied" else "rejected"
        updated = conn.execute(
            """UPDATE project_bug_operations
            SET state=?,result_json=?,updated_at=?
            WHERE operation_id=? AND state='unknown'""",
            (state, result_json, iso_now(), operation_id),
        )
        if updated.rowcount != 1:
            raise BugConflict("operation changed during settlement")
        conn.execute(
            "INSERT INTO project_bug_operation_observations VALUES(?,?,?,?)",
            (new_id("pboo"), operation_id, result_json, iso_now()),
        )
        _event(
            conn,
            op["bug_id"],
            actor,
            "write_settled_by_human",
            {
                "operation_id": operation_id,
                "settlement_id": settlement_id,
                "verdict": verdict,
            },
        )
        return projection(conn, operation_id)
