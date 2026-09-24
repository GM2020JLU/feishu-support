"""Read back a created draft through the existing scoped intake queue.

Creation receipts remain distinct from verified snapshots. This action never
creates a remote item, and a failed read cannot trigger another creation.
"""

from . import project_bug_create as drafts
from . import project_link_intake as intake
from .ids import digest
from .project_intake_policy import validate


def enqueue(conn, config, *, actor, draft_id, expected_digest, read_hours, local_priority,
            retry_intake_id=None):
    row = drafts._owned(conn, draft_id, actor)
    drafts._expect(row, expected_digest)
    if row["state"] != "created" or not row["created_item_id"]:
        raise ValueError("creation must be settled before binding and readback")
    spaces = validate(config.raw.get("project_integration", {}).get("intake_spaces", []))
    matches = [s for s in spaces
               if row["project_key"] in {s["simple_name"], s["project_key"]}
               and (row["type_key"] in s["type_keys"]
                    or row["type_key"] in s.get("type_aliases", {}))]
    if len(matches) != 1:
        raise PermissionError("created item has no unambiguous configured intake scope")
    space = matches[0]
    url = f'https://{row["host"]}/{space["simple_name"]}/{row["type_key"]}/detail/{row["created_item_id"]}'
    # Stable across double clicks, page reloads and request-response loss. Changing
    # read duration deliberately does not silently renew a previous request.
    request_id = "created-draft:" + digest({"draft_id": draft_id, "actor": actor})
    if retry_intake_id is not None:
        previous = conn.execute(
            "SELECT * FROM project_link_intakes WHERE intake_id=? AND actor=?",
            (retry_intake_id, actor),
        ).fetchone()
        if (previous is None or previous["url"] != url
                or not (previous["request_id"] == request_id
                        or previous["request_id"].startswith(request_id + ":retry:"))):
            raise PermissionError("read retry does not belong to this created draft")
        if previous["state"] not in {"failed", "blocked"}:
            raise ValueError("only a terminal failed read may be retried")
        # One successor per failed attempt: response loss and double-clicks reuse
        # it. Current scope/mode/reader checks still run in intake.enqueue.
        request_id += ":retry:" + digest(retry_intake_id)
    return intake.enqueue(conn, config, actor=actor, url=url, request_id=request_id,
                          read_hours=read_hours, local_priority=local_priority)
