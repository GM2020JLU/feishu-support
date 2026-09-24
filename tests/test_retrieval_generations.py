"""Explicit owner delegation is a new lookup, not a replay of old evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from k3_support.config import Config, validate_config
from k3_support.control import ControlMessage, execute_control
from k3_support.coordination import ensure_turn
from k3_support.retrieval import (
    create_retrieval_job,
    retrieval_input_for_case,
    run_retrieval_job,
)
from k3_support.store import claim_jobs, create_case, ingest_event


def setup_lookup(conn, config):
    data = dict(config.raw)
    data["mode"] = "active"
    data["features"] = {**data["features"], "codex": True}
    cfg = Config(validate_config(data), config.path)
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_generations",
        payload={"content": "K3 风扇温控如何配置", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_lookup",
    )
    cid, _ = create_case(
        conn,
        title="Synthetic fresh lookup",
        case_type="faq",
        severity="P3",
        confidence=0.9,
        requester_id="ou_colleague",
        requester_chat_id="oc_lookup",
        source_event_pk=event_pk,
    )
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (cid,))
    ensure_turn(conn, case_id=cid, source_event_pk=event_pk)
    current = retrieval_input_for_case(conn, case_id=cid, source_event_pk=event_pk)
    old, _ = create_retrieval_job(
        conn,
        cfg,
        case_id=cid,
        query=current["full_query"],
        source_event_pk=current["source_event_pk"],
        context_binding=current["context_binding"],
    )
    conn.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (old,))
    return cfg, cid, old


def test_new_owner_delegation_never_reuses_successful_lookup(conn, config):
    cfg, cid, old = setup_lookup(conn, config)
    before = dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (old,)).fetchone())
    command = ControlMessage(
        "owner-user", "owner-chat", "new-fresh-request", f"delegate {cid}"
    )
    result = execute_control(conn, cfg, command)
    assert result["continuation"]["created"] is True
    fresh = result["continuation"]["job_id"]
    assert fresh != old
    assert [j["job_id"] for j in claim_jobs(conn, "lookup-worker")] == [fresh]
    assert (
        dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (old,)).fetchone())
        == before
    )
    assert execute_control(conn, cfg, command)["replayed"]
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2


def test_fresh_lookup_keeps_auditable_generation_and_runs_real_retrieval_path(
    conn, config
):
    from k3_support.lark import CommandResult

    cfg, cid, old = setup_lookup(conn, config)
    result = execute_control(
        conn,
        cfg,
        ControlMessage("owner-user", "owner-chat", "run-generation", f"delegate {cid}"),
    )
    fresh = result["continuation"]["job_id"]
    assert fresh != old
    job = claim_jobs(conn, "lookup-worker")[0]
    context = json.loads(job["context_json"])
    assert len(context["request_generation"]) == 64
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[:2] == ["drive", "+search"]:
            return CommandResult({"results": [], "has_more": False}, "user", [])
        return CommandResult({"messages": [], "has_more": False}, "user", [])

    run_retrieval_job(conn, cfg, job_id=fresh, runner=runner)
    assert len(calls) == 2
    assert all(argv[-2:] == ["--as", "user"] for argv in calls)
