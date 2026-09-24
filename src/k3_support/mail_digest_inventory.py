"""Authenticated console views of frozen mail summaries; no delivery effects."""

import json

from .mail_snapshot import query_summary


def page(conn, *, after_id=""):
    if not isinstance(after_id, str) or len(after_id) > 128:
        raise ValueError("invalid summary cursor")
    params, where = [], ""
    if after_id:
        anchor = conn.execute("SELECT created_at,digest_id FROM mail_digest_runs WHERE digest_id=?", (after_id,)).fetchone()
        if anchor is None:
            raise ValueError("summary cursor no longer exists; refresh")
        where = "WHERE (created_at,digest_id) < (?,?)"
        params.extend(anchor)
    rows = conn.execute(
        "SELECT digest_id,range_end,item_count,state,notification_channel,created_at "
        f"FROM mail_digest_runs {where} ORDER BY created_at DESC,digest_id DESC LIMIT 31", params,
    ).fetchall()
    return {"items": [dict(row) for row in rows[:30]],
            "next_cursor": rows[29]["digest_id"] if len(rows) > 30 else None, "read_only": True}


def detail(conn, *, digest_id, page=1, expected_digest=None):
    if not isinstance(digest_id, str) or not digest_id or len(digest_id) > 128:
        raise ValueError("invalid summary ID")
    if type(page) is not int or (page > 1 and not expected_digest):
        raise ValueError("summary pagination requires a snapshot digest")
    result = query_summary(conn, digest_id=digest_id, page=page, expected_digest=expected_digest)
    row = conn.execute("SELECT ai_summary_json,notification_channel FROM mail_digest_runs WHERE digest_id=?", (digest_id,)).fetchone()
    result["assessment"] = json.loads(row["ai_summary_json"])
    result["notification_channel"] = row["notification_channel"]
    return result
