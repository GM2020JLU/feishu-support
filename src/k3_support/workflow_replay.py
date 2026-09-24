"""Actual inbound orchestration on an explicitly supplied memory snapshot.

Internal building block, not a sandbox or public live-data replay command.
Model callbacks must be trusted/injected; they can themselves perform I/O.
No consumers are run: queued messages and jobs are captured as intentions.
"""
from __future__ import annotations

import copy
import sqlite3
from typing import Any

from .config import Config
from .conversation_context import admit_im_event
from .coordination import control_communication
from .inbound_claims import InboundClaimLost
from .orchestrator import complete_research_route, process_inbound
from .store import ingest_event


class ReplayBoundaryError(RuntimeError):
    pass


def _process_for_replay(conn, **kwargs):
    try:
        return process_inbound(conn, **kwargs)
    except InboundClaimLost:
        state = conn.execute("SELECT mode FROM global_control_state WHERE scope='feishu_support'").fetchone()
        if state is None or state['mode'] not in {'paused', 'stopped'}:
            raise
        return {'processed': False, 'blocked_by_mode': state['mode'], 'case_id': None}


def deny_transport(*args, **kwargs):
    raise ReplayBoundaryError("external lookup is unavailable in workflow replay")


def require_memory(conn: sqlite3.Connection) -> None:
    if any(row[2] for row in conn.execute("PRAGMA database_list")):
        raise ReplayBoundaryError("workflow replay requires memory-only databases")


def replay_research_completion(conn, config, *, case_id, retrieval_result,
                               selector, clarification_reviewer=None):
    """Continue a snapshot's actual retrieval result without running consumers.

    Production completion verifies source/authority freshness. Supplied callbacks
    are trusted transports, not sandboxed here. Results may contain private text.
    """
    require_memory(conn)
    from .content_retirement import require_case_content
    require_case_content(conn, case_id=case_id)
    tables = (("outbox", "outbox_id"), ("jobs", "job_id"))
    before = {table: {row[0] for row in conn.execute(f"SELECT {key} FROM {table}")}
              for table, key in tables}
    result = complete_research_route(conn, config, case_id=case_id,
        retrieval_result=copy.deepcopy(retrieval_result), selector=selector,
        clarification_reviewer=clarification_reviewer)
    intentions = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")
                          if row[key] not in before[table]] for table, key in tables}
    return {"result": result, "intentions": intentions,
            "execution_scope": "research_completion_no_consumers",
            "model_quality_verified": False}


def replay_communication(conn: sqlite3.Connection, *, case_id: str, action: str,
                         actor_id: str, external_id: str) -> dict[str, Any]:
    """Apply a simulated owner choice through the real communication control.

    Actor identity is scenario input, not authenticated owner authorization.
    Consequently this must never be used as a live control endpoint.
    """
    require_memory(conn)
    return control_communication(conn, case_id=case_id, action=action,
                                 actor_id=actor_id, external_id=external_id)


def replay_inbound(
    conn: sqlite3.Connection, config: Config, event: dict[str, Any], *,
    message_router, semantic_selector=None, clarification_reviewer=None,
    similarity_selector=None, diagnostic_extractor=None, meeting_planner=None,
) -> dict[str, Any]:
    """Process one new event, retaining history across calls on the same copy.

    Refuse disk connections (including attached databases) before any write.
    Existing external IDs are rejected rather than resetting historical claims.
    Results contain sensitive bodies and must not be logged/exported by default.
    """
    require_memory(conn)
    allowed = {
        "source", "identity", "external_id", "payload", "occurred_at",
        "sender_id", "chat_id", "thread_id",
    }
    if set(event) - allowed:
        raise ValueError("unsupported replay event fields")
    if conn.execute(
        "SELECT 1 FROM inbound_events WHERE source=? AND identity=? AND external_id=?",
        (event["source"], event["identity"], event["external_id"]),
    ).fetchone():
        raise ValueError("replay event identity already exists")
    if event['source'] in {'feishu_user_poll', 'feishu_bot_im'}:
        event_pk, _ = admit_im_event(conn, config, copy.deepcopy(event))
        if event_pk is None:
            return {'result': {'ignored': True, 'reason': 'outside_current_im_scope', 'case_id': None},
                    'intentions': {'outbox': [], 'jobs': []}, 'event_pk': None,
                    'execution_scope': 'inbound_only_no_consumers', 'model_quality_verified': False}
    else:
        event_pk, _ = ingest_event(conn, **copy.deepcopy(event))
    return resume_inbound(conn, config, event_pk=event_pk, message_router=message_router,
        semantic_selector=semantic_selector, clarification_reviewer=clarification_reviewer,
        similarity_selector=similarity_selector, diagnostic_extractor=diagnostic_extractor,
        meeting_planner=meeting_planner)


def resume_inbound(conn, config, *, event_pk, message_router, semantic_selector=None,
                   clarification_reviewer=None, similarity_selector=None,
                   diagnostic_extractor=None, meeting_planner=None):
    """Retry the stored event through normal claims, without resetting its state."""
    require_memory(conn)
    if not conn.execute('SELECT 1 FROM inbound_events WHERE event_pk=?', (event_pk,)).fetchone():
        raise ValueError('replay event not found')
    before = {
        table: {row[0] for row in conn.execute(f"SELECT {key} FROM {table}")}
        for table, key in (("outbox", "outbox_id"), ("jobs", "job_id"))
    }
    result = _process_for_replay(
        conn, event_pk=event_pk, worker_id="workflow-replay", config=config,
        message_router=message_router, semantic_selector=semantic_selector,
        clarification_reviewer=clarification_reviewer,
        similarity_selector=similarity_selector,
        diagnostic_extractor=diagnostic_extractor, meeting_planner=meeting_planner,
        contact_runner=deny_transport, calendar_runner=deny_transport,
    )
    intentions = {}
    for table, key in (("outbox", "outbox_id"), ("jobs", "job_id")):
        intentions[table] = [
            dict(row) for row in conn.execute(f"SELECT * FROM {table}")
            if row[key] not in before[table]
        ]
    return {
        "result": result, "intentions": intentions, "event_pk": event_pk,
        "execution_scope": "inbound_only_no_consumers",
        "model_quality_verified": False,
    }
