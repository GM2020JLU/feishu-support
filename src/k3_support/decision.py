from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import nullcontext
from typing import Any

from .config import Config
from .coordination import bind_ai_communication
from .db import transaction
from .ids import new_id
from .message_format import format_feishu_ai_message
from .state_machine import require_transition
from .store import ConflictError, NotFoundError, enqueue_outbox
from .timeutil import epoch_now, iso_now


class DecisionError(ValueError):
    pass


INTENTS = {"reply", "retrieve", "delegate_codex", "request_board", "request_push", "wait", "escalate"}
ACTIONS = {"feishu_reply", "telegram_notify", "create_retrieval_job", "create_codex_job", "request_board", "request_push"}
REQUIRED_FIELDS = {
    "decision_id",
    "case_id",
    "expected_case_version",
    "intent",
    "confidence",
    "evidence_ids",
    "reply_draft",
    "proposed_actions",
    "facts",
    "inferences",
    "unknowns",
}


def _forbidden_reply_paths(
    config: Config | None, extra: Iterable[str] = ()
) -> tuple[str, ...]:
    paths = {"/home/operator", "/data/home2/operator"}
    paths.update(path for path in extra if isinstance(path, str) and path.startswith("/"))
    if config is not None:
        paths.add(str(config.data_dir.resolve()))
        paths.update(
            config.raw["runtime"][name]
            for name in (
                "remote_workspace_root",
                "remote_source_root",
                "remote_worktree_root",
            )
            if config.raw["runtime"].get(name)
        )
    return tuple(sorted(paths, key=len, reverse=True))


def validate_decision(
    value: Any,
    *,
    config: Config | None = None,
    forbidden_paths: Iterable[str] = (),
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != REQUIRED_FIELDS:
        raise DecisionError("decision fields do not match schema")
    if not isinstance(value["decision_id"], str) or not value["decision_id"]:
        raise DecisionError("decision_id must be a non-empty string")
    if not isinstance(value["case_id"], str) or not value["case_id"].startswith("K3-"):
        raise DecisionError("invalid case_id")
    if not isinstance(value["expected_case_version"], int) or value["expected_case_version"] < 1:
        raise DecisionError("expected_case_version must be positive")
    if value["intent"] == "resolve":
        raise DecisionError("problem resolution requires authenticated operator confirmation")
    if value["intent"] not in INTENTS:
        raise DecisionError("intent is not allowed")
    if not isinstance(value["confidence"], (int, float)) or not 0 <= value["confidence"] <= 1:
        raise DecisionError("confidence must be between 0 and 1")
    for key in ("evidence_ids", "proposed_actions", "facts", "inferences", "unknowns"):
        if not isinstance(value[key], list):
            raise DecisionError(f"{key} must be a list")
    if value["reply_draft"] is not None and not isinstance(value["reply_draft"], str):
        raise DecisionError("reply_draft must be a string or null")
    for action in value["proposed_actions"]:
        if not isinstance(action, dict) or action.get("type") not in ACTIONS:
            raise DecisionError("proposed action is not allowed")
        if "shell" in action or "sql" in action or "identity" in action:
            raise DecisionError("decision cannot carry shell, SQL, or identity")
    if value["intent"] == "reply":
        draft = value["reply_draft"] or ""
        if not value["evidence_ids"]:
            raise DecisionError("automatic reply requires at least one evidence ID")
        if len(value["evidence_ids"]) != len(set(value["evidence_ids"])):
            raise DecisionError("evidence IDs must be unique")
        if value["confidence"] < 0.85 or not draft.startswith("[AI 自动回复]"):
            raise DecisionError("automatic reply fails confidence or visible-prefix gate")
        if any(path in draft for path in _forbidden_reply_paths(config, forbidden_paths)):
            raise DecisionError("automatic reply exposes an internal absolute path")
    return value


def _target_state(intent: str, before: str) -> str | None:
    return {
        "reply": "answering",
        "retrieve": "investigating",
        "delegate_codex": "investigating",
        "request_board": "waiting_board",
        "request_push": "waiting_push",
        "escalate": "escalated",
    }.get(intent)


def apply_decision(
    conn: sqlite3.Connection, value: Any, *, config: Config | None = None
) -> dict[str, Any]:
    decision = validate_decision(value, config=config)
    case_id = decision["case_id"]
    with (nullcontext(conn) if conn.in_transaction else transaction(conn)):
        duplicate = conn.execute(
            "SELECT detail_json FROM case_events WHERE idempotency_key=?", (f"decision:{decision['decision_id']}",)
        ).fetchone()
        if duplicate:
            return {"case_id": case_id, "applied": False, "reason": "duplicate"}
        row = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError(case_id)
        if row["version"] != decision["expected_case_version"]:
            raise ConflictError(
                f"stale case version: expected {decision['expected_case_version']}, current {row['version']}"
            )
        evidence_rows: list[sqlite3.Row] = []
        if decision["evidence_ids"]:
            evidence_rows = conn.execute(
                f"""SELECT e.evidence_id,e.source_id,cs.requester_access,cs.source_type,cs.stable_external_id
                    FROM evidence e LEFT JOIN case_sources cs ON cs.source_id=e.source_id
                    WHERE e.case_id=? AND e.evidence_id IN
                    ({','.join('?' for _ in decision['evidence_ids'])})""",
                (case_id, *decision["evidence_ids"]),
            ).fetchall()
            if len(evidence_rows) != len(set(decision["evidence_ids"])):
                raise DecisionError("decision references unavailable evidence")
            if decision["intent"] == "reply" and any(
                item["source_id"] is None or item["requester_access"] != "allowed"
                for item in evidence_rows
            ):
                raise DecisionError("reply evidence is not disclosure-safe for the requester")

        before = str(row["state"])
        after = _target_state(decision["intent"], before)
        if after == before:
            after = None
        if after is not None:
            require_transition(before, after)

        outbox_ids: list[str] = []
        reply_basis = (
            "approved_knowledge"
            if decision["evidence_ids"]
            and any(item["source_type"] == "approved_knowledge" for item in evidence_rows)
            else "verified_evidence"
        )
        for index, action in enumerate(decision["proposed_actions"]):
            action_type = action["type"]
            if action_type == "feishu_reply":
                if decision["intent"] != "reply":
                    raise DecisionError("feishu_reply requires reply intent")
                destination = action.get("source_message_id")
                source_event_pk = action.get("source_event_pk")
                if not isinstance(destination, str) or not destination or not isinstance(source_event_pk, str):
                    raise DecisionError("feishu_reply needs source_message_id and source_event_pk")
                source = conn.execute(
                    "SELECT source,identity,external_id FROM inbound_events WHERE event_pk=?", (source_event_pk,)
                ).fetchone()
                linked = conn.execute(
                    "SELECT 1 FROM case_events WHERE case_id=? AND source_event_pk=?",
                    (case_id, source_event_pk),
                ).fetchone()
                if (
                    source is None
                    or linked is None
                    or source["source"] not in {"feishu_bot_im", "feishu_user_poll"}
                    or source["external_id"] != destination
                    or source["identity"] not in {"bot", "user"}
                ):
                    raise DecisionError("reply source identity or message does not match the immutable event")
                binding = (
                    bind_ai_communication(
                        conn,
                        config,
                        case_id=case_id,
                        source_event_pk=source_event_pk,
                    )
                    if config is not None
                    else None
                )
                if config is not None and binding is None:
                    raise DecisionError("AI no longer owns communication for this turn")
                reply_text = format_feishu_ai_message(decision["reply_draft"])
                release_binding = None
                if reply_basis == "approved_knowledge" and config is not None:
                    from .knowledge_release import bind_knowledge_reply

                    release_binding = bind_knowledge_reply(
                        conn, config, source_event_pk=source_event_pk,
                        knowledge_ids=sorted({str(item["stable_external_id"]) for item in evidence_rows
                                              if item["source_type"] == "approved_knowledge"}),
                        text=reply_text,
                    )
                outbox_id, _ = enqueue_outbox(
                    conn,
                    channel="feishu_im",
                    action_type="reply",
                    destination=destination,
                    payload={
                        "text": reply_text,
                        "identity": source["identity"],
                        "reply_basis": reply_basis,
                        "format": "markdown",
                        "evidence_ids": decision["evidence_ids"],
                        **({"knowledge_release": release_binding} if release_binding is not None else {}),
                    },
                    idempotency_key=f"decision:{decision['decision_id']}:action:{index}",
                    case_id=case_id,
                    source_event_pk=source_event_pk,
                    **(binding or {}),
                )
                outbox_ids.append(outbox_id)
            elif action_type == "telegram_notify":
                destination = action.get("destination")
                text = action.get("text")
                if not isinstance(destination, str) or not isinstance(text, str):
                    raise DecisionError("telegram_notify needs destination and text")
                outbox_id, _ = enqueue_outbox(
                    conn,
                    channel="telegram",
                    action_type="notify",
                    destination=destination,
                    payload={"text": text},
                    idempotency_key=f"decision:{decision['decision_id']}:action:{index}",
                    case_id=case_id,
                )
                outbox_ids.append(outbox_id)
            else:
                raise DecisionError(f"action {action_type} requires a dedicated deterministic command")

        now = iso_now()
        version = int(row["version"])
        if after is not None:
            conn.execute(
                "UPDATE cases SET state=?,version=version+1,updated_at=?,updated_epoch=? WHERE case_id=? AND version=?",
                (after, now, epoch_now(), case_id, version),
            )
            version += 1
        sequence = conn.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        import json
        conn.execute(
            """INSERT INTO case_events(event_id,case_id,sequence,event_type,actor_type,before_state,
                   after_state,detail_json,idempotency_key,created_at,created_epoch)
               VALUES(?,?,?,'decision_applied','hermes',?,?,?,?,?,?)""",
            (
                new_id("cev"),
                case_id,
                sequence,
                before,
                after or before,
                json.dumps({"decision": decision, "outbox_ids": outbox_ids}, ensure_ascii=False, sort_keys=True),
                f"decision:{decision['decision_id']}",
                now,
                epoch_now(),
            ),
        )
        return {"case_id": case_id, "applied": True, "version": version, "outbox_ids": outbox_ids}
