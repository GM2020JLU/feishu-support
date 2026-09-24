"""Read-only execution projections, never proof that hardware is idle or safe."""

import json
import re

from .board_cleanup_status import items as cleanup_items
from .board_cleanup_status import pending as cleanup_pending
from .timeutil import iso_now, parse_iso

_AGENT_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "dsh": "DeepSeek Harness",
    "opencode": "OpenCode",
    "hermes": "Hermes",
}


def _public_job(row):
    from .broker_execution_contract import validate_selection

    item = dict(row)
    raw = item.pop("_execution_context")
    if item["job_type"] != "codex":
        return item
    identity = {"binding": "unknown"}
    if item["content_retired"]:
        identity = {"binding": "retired"}
    elif isinstance(raw, str) and len(raw) <= 65536:
        try:
            context = json.loads(raw)
            if not isinstance(context, dict):
                raise TypeError("invalid context")
            agent = context.get("agent")
            if "execution" in context:
                selected = validate_selection(context["execution"])
                if selected["agent"] != agent:
                    raise ValueError("inconsistent identity")
                binding = "deployment_contract"
            elif agent == "codex":
                binding = "legacy"
            else:
                raise ValueError("unbound agent")
            model, reasoning = context.get("model"), context.get("reasoning")
            if any(
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value)
                for value in (model, reasoning)
            ):
                raise ValueError("invalid model identity")
            identity = {
                "binding": binding,
                "agent": agent,
                "label": _AGENT_LABELS[agent],
                "model": model,
                "reasoning": reasoning,
            }
        except (ValueError, TypeError, KeyError):
            pass
    item["coding_identity"] = identity
    return item


def _remote_pending(conn, job_id):
    from .broker_remote_state import UNSETTLED

    return [
        dict(row)
        for row in conn.execute(
            "SELECT a.request_id,a.updated_at,CASE WHEN json_valid(a.plan_json) THEN "
            "json_extract(a.plan_json,'$.guard_version')=2 ELSE 0 END AS observation_available "
            "FROM broker_remote_actions a JOIN broker_grants g USING(grant_id) "
            "LEFT JOIN broker_remote_results r USING(request_id) "
            f"WHERE g.job_id=? AND a.state='unknown' AND {UNSETTLED} "
            "ORDER BY a.created_at,a.request_id LIMIT 5",
            (job_id,),
        )
    ]


def page(conn, config, *, state="active", after_id="", limit=30):
    if state not in ("active", "all", "failed", "succeeded", "cancelled"):
        raise ValueError("invalid execution filter")
    if (
        not isinstance(after_id, str)
        or len(after_id) > 100
        or type(limit) is not int
        or not 1 <= limit <= 50
    ):
        raise ValueError("invalid execution page")
    board = config.raw["policy"]["board_alias"]
    now = iso_now()
    where = "job_type IN ('codex','build','board','push') AND (?='all' OR state=? OR (?='active' AND state IN ('queued','running','waiting','orphaned')))"
    args = (state, state, state)
    retired = (
        "EXISTS(SELECT 1 FROM case_content_retirements retired "
        "WHERE retired.case_id=jobs.case_id AND retired.lifecycle_round=jobs.lifecycle_round)"
    )
    conn.execute("SAVEPOINT execution_inventory")
    try:
        count = conn.execute(
            "SELECT count(*) FROM jobs WHERE " + where, args
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT job_id,case_id,job_type,state,"
            f"CASE WHEN {retired} THEN 'case_content_retired' ELSE error_class END AS error_class,"
            "heartbeat_at,lease_expires_at,session_id,exit_code,updated_at,"
            f"CASE WHEN {retired} THEN NULL ELSE context_json END AS _execution_context,"
            f"({retired}) AS content_retired,"
            f"(NOT ({retired}) AND job_type='codex' AND state='waiting' AND error_class IN ('broker_input_invalid','broker_budget_blocked')) AS input_recovery_available FROM jobs WHERE "
            + where
            + " AND job_id>? ORDER BY job_id LIMIT ?",
            (*args, after_id, limit + 1),
        ).fetchall()
        lock = conn.execute(
            "SELECT case_id,scope,acquired_at,expires_at,heartbeat_at FROM locks WHERE lock_key=?",
            (board,),
        ).fetchone()
        lease = dict(lock) if lock else None
        if lease:
            try:
                lease["expiry_state"] = (
                    "expired"
                    if parse_iso(lease["expires_at"]) <= parse_iso(now)
                    else "unexpired"
                )
            except (ValueError, TypeError):
                lease["expiry_state"] = "unknown"
        from .execution_stop import status as stop_status

        return {
            "items": [
                {
                    **_public_job(row),
                    "stop": stop_status(conn, job_id=row["job_id"]),
                    "remote_pending": _remote_pending(conn, row["job_id"]),
                    "board_cleanup": cleanup_items(conn, job_id=row["job_id"]),
                }
                for row in rows[:limit]
            ],
            "total_matching": count,
            "next_cursor": rows[limit - 1]["job_id"] if len(rows) > limit else None,
            "board": {
                "name": board,
                "lease": lease,
                "physical_state": "unknown",
                "cleanup_pending": cleanup_pending(conn),
            },
            "observed_at": now,
            "read_only": True,
            "live": True,
        }
    finally:
        conn.execute("RELEASE execution_inventory")
