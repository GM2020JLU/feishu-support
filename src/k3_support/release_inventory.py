"""Read-only, bounded projection of recorded release assessments."""

import json
import sqlite3


def page(conn: sqlite3.Connection, *, repository: str = "", after_id: str = "", limit: int = 30) -> dict:
    if not isinstance(repository, str) or len(repository) > 500:
        raise ValueError("invalid repository")
    if not isinstance(after_id, str) or len(after_id) > 500:
        raise ValueError("invalid cursor")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("invalid page size")
    rows = conn.execute(
        """SELECT impact_id,repository,change_id,revision,subject,branch,created_at,
                  assessment_json FROM release_impacts
             WHERE (?='' OR repository=?) AND impact_id>?
             ORDER BY impact_id LIMIT ?""",
        (repository, repository, after_id, limit + 1),
    ).fetchall()
    items = []
    for row in rows[:limit]:
        value = dict(row)
        assessment = json.loads(value.pop("assessment_json"))
        assessment = assessment if isinstance(assessment, dict) else {}
        summary = assessment.get("summary")
        level = assessment.get("impact_level")
        value.update(summary=summary[:4000] if isinstance(summary, str) else "评估摘要不可用",
                     impact_level=level if isinstance(level, str) and level in {"low", "medium", "high"} else "unknown")
        items.append(value)
    return {"items": items, "next_after_id": items[-1]["impact_id"] if len(rows) > limit else None,
            "note": "已有本地评估，不代表版本已发布、完成实测或通知已送达。"}
