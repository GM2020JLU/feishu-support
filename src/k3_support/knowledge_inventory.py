"""Operator-only lexical inventory, separate from requester semantic retrieval."""


def page(conn, *, query="", status="all", after_id="", limit=30):
    if not isinstance(query, str) or len(query) > 256:
        raise ValueError("knowledge query exceeds limit")
    if not isinstance(status, str) or status not in {
        "all",
        "candidate",
        "approved",
        "stale",
        "retired",
    }:
        raise ValueError("invalid knowledge status")
    if (
        not isinstance(after_id, str)
        or len(after_id) > 100
        or type(limit) is not int
        or not 1 <= limit <= 50
    ):
        raise ValueError("invalid knowledge page")
    query = query.strip()
    where = "(?='all' OR status=?) AND (?='' OR instr(lower(coalesce(title,'')||' '||coalesce(project,'')||' '||coalesce(module,'')||' '||coalesce(answer_markdown,'')),lower(?))>0)"
    params = (status, status, query, query)
    # One read snapshot for matching count and page. No lifecycle changes.
    conn.execute("SAVEPOINT knowledge_inventory_read")
    try:
        count = conn.execute(
            "SELECT count(*) FROM knowledge_entries WHERE " + where, params
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT knowledge_id,title,status,project,module,review_due_at FROM knowledge_entries WHERE "
            + where
            + " AND knowledge_id>? ORDER BY knowledge_id LIMIT ?",
            (*params, after_id, limit + 1),
        ).fetchall()
        return {
            "items": [dict(row) for row in rows[:limit]],
            "total_matching": count,
            "next_cursor": rows[limit - 1]["knowledge_id"]
            if len(rows) > limit
            else None,
            "query": query,
            "status": status,
            "live": True,
            "semantic_search": False,
        }
    finally:
        conn.execute("RELEASE knowledge_inventory_read")
