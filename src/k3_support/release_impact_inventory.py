"""Read-only, bounded inventory of persisted source-change assessments."""

import json


def page(conn, *, after_id=""):
    if not isinstance(after_id, str) or len(after_id) > 128:
        raise ValueError("invalid impact cursor")
    params = []
    where = ""
    if after_id:
        previous = conn.execute(
            "SELECT created_at,impact_id FROM release_impacts WHERE impact_id=?", (after_id,)
        ).fetchone()
        if previous is None:
            raise ValueError("impact cursor no longer exists; refresh the list")
        where = "WHERE (created_at,impact_id) < (?,?)"
        params.extend(previous)
    rows = conn.execute(
        f"SELECT impact_id,repository,change_id,revision,subject,created_at "
        f"FROM release_impacts {where} ORDER BY created_at DESC,impact_id DESC LIMIT 31",
        params,
    ).fetchall()
    return {"items": [dict(row) for row in rows[:30]],
            "next_cursor": rows[29]["impact_id"] if len(rows) > 30 else None,
            "read_only": True}


def detail(conn, *, impact_id):
    if not isinstance(impact_id, str) or not impact_id or len(impact_id) > 128:
        raise ValueError("invalid impact ID")
    row = conn.execute("SELECT * FROM release_impacts WHERE impact_id=?", (impact_id,)).fetchone()
    if row is None:
        raise ValueError("impact assessment not found")
    result = dict(row)
    result["changed_paths"] = json.loads(result.pop("changed_paths_json"))
    result["assessment"] = json.loads(result.pop("assessment_json"))
    result["read_only"] = True
    return result
