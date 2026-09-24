"""Stage operator-reviewed result comments; never send or infer a repair verdict."""

from . import project_bug_operations as operations
from .db import atomic
from .ids import digest
from .project_bugs import BugConflict
from .project_investigation_result import draft

FIELDS = {"bug_id", "job_id", "result_digest", "snapshot_id", "expected_revision",
          "grant_id", "request_id", "text"}


def require_source(conn, *, bug_id, job_id, result_digest):
    result = draft(conn, bug_id=bug_id, job_id=job_id)
    if (result["archived"] or result["report"]["state"] != "available"
            or result["report"]["digest"] != result_digest):
        raise BugConflict("result changed or investigation archived; review the current report")


def guard(conn, operation):
    source = conn.execute("SELECT * FROM project_result_comments WHERE operation_id=?",
                          (operation["operation_id"],)).fetchone()
    if source is not None:
        require_source(conn, bug_id=operation["bug_id"], job_id=source["job_id"],
                       result_digest=source["result_digest"])


def prepare(conn, *, actor, payload):
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ValueError("result comment requires exact fields")
    signature = digest(payload)
    with atomic(conn):
        old = conn.execute("SELECT * FROM project_bug_operations WHERE actor=? AND request_id=?",
                           (actor, payload["request_id"])).fetchone()
        if old is not None:
            source = conn.execute("SELECT request_digest FROM project_result_comments WHERE operation_id=?",
                                  (old["operation_id"],)).fetchone()
            if source is None or source[0] != signature:
                raise BugConflict("result comment request ID reused for different content")
            return dict(old)
        require_source(conn, **{key:payload[key] for key in ("bug_id", "job_id", "result_digest")})
        operation = operations.prepare(conn, actor=actor, action="bug.comment",
                                       change={"text":payload["text"]},
                                       **{key:payload[key] for key in (
                                           "bug_id", "snapshot_id", "expected_revision", "grant_id", "request_id")})
        conn.execute("INSERT INTO project_result_comments VALUES(?,?,?,?)",
                     (operation["operation_id"], payload["job_id"], payload["result_digest"], signature))
        return operation
