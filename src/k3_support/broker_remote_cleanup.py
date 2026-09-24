"""Operator-confirmed remote occupancy release, not repair or execution success."""

import hashlib
import json
import os
import shlex
from datetime import UTC, datetime, timedelta
from uuid import UUID

from .broker_remote_observation import _snapshot
from .db import transaction
from .execution_transport import matches
from .ids import canonical_json
from .remote_guard import receipt_directory, wrap
from .timeutil import iso_now


def _candidate(conn, config, request_id, observation_id):
    for value in (request_id, observation_id):
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("canonical evidence identifiers required")
    action = conn.execute("SELECT * FROM broker_remote_actions WHERE request_id=?", (request_id,)).fetchone()
    audit = conn.execute("SELECT * FROM broker_remote_observations WHERE observation_id=?", (observation_id,)).fetchone()
    if not action or action["state"] != "unknown" or not audit or audit["request_id"] != request_id or audit["state"] != "observed":
        raise ValueError("observed unknown remote execution required")
    if audit["snapshot_digest"] != _snapshot(conn, config, request_id):
        raise ValueError("observation snapshot stale")
    if not isinstance(audit["finished_at"], str):
        raise ValueError("finished observation required")  # noqa: TRY004 - invalid persisted observation, not a caller type error
    finished = datetime.fromisoformat(audit["finished_at"])
    now = datetime.now(UTC)
    if finished.tzinfo is None or not now-timedelta(minutes=10) <= finished <= now:
        raise ValueError("fresh observation required")
    manager = conn.execute("SELECT e.* FROM broker_service_exits e JOIN broker_execution_instances i "
                           "ON e.grant_id=i.grant_id AND e.invocation_id=i.invocation_id WHERE e.grant_id=?",
                           (action["grant_id"],)).fetchone()
    if manager is None:
        raise ValueError("worker service exit must be independently observed first")
    plan = json.loads(action["plan_json"])
    directory = receipt_directory(config.raw["runtime"])
    if (not isinstance(plan, dict) or not directory or type(plan.get("guard_version")) is not int or plan["guard_version"] != 2
            or plan.get("receipt_directory") != directory or not matches(config, plan)):
        raise ValueError("receipt execution plan changed")
    argv = shlex.split(plan["command"])
    if (len(argv) != 8 or wrap(argv[5], receipt_directory=directory, request_id=request_id) != plan["command"]
            or hashlib.sha256(argv[5].encode()).hexdigest() != plan.get("command_digest")):
        raise ValueError("current trusted guardian implementation required")
    observed = json.loads(audit["result_json"])
    expected = {"state": "guardian_returned", "version": 1, "request_id": request_id,
                "command_digest": plan["command_digest"], "read_only": True, "recovery_authorized": False}
    if (not isinstance(observed, dict) or set(observed) != {*expected, "guard_exit_code"}
            or any(type(observed[k]) is not type(v) or observed[k] != v for k, v in expected.items())
            or type(observed["guard_exit_code"]) is not int or not 0 <= observed["guard_exit_code"] <= 255):
        raise ValueError("receipt binding invalid")
    local = conn.execute("SELECT exit_code FROM broker_remote_results WHERE request_id=?", (request_id,)).fetchone()
    code = local[0] if local else None
    if code is not None and 0 <= code < 255 and code not in (124, 125) and code != observed["guard_exit_code"]:
        raise ValueError("contradictory execution exit evidence")
    digest = hashlib.sha256(canonical_json({"audit": dict(audit), "action": dict(action),
                                          "manager": dict(manager), "local_exit": code,
                                          "snapshot": audit["snapshot_digest"]}).encode()).hexdigest()
    return action, code, digest


def preview(conn, config, *, request_id, observation_id):
    with transaction(conn, immediate=False):
        _, _, digest = _candidate(conn, config, request_id, observation_id)
    return {"request_id": request_id, "observation_id": observation_id, "preview_digest": digest,
            "summary": "仅解除本次远端占用；执行结果仍未知，不重跑、不确认修复、不发送回复。"}


def apply(conn, config, *, request_id, observation_id, preview_digest, panel_token=None, panel_identity=None):
    with transaction(conn):
        if panel_token is not None:
            panel = conn.execute("SELECT * FROM broker_remote_cleanup_panels WHERE token=?", (panel_token,)).fetchone()
            if (not panel or panel["cancelled"] or
                    (panel["user_id"], panel["chat_id"], panel["prompt_message_id"]) != panel_identity or
                    (panel["request_id"], panel["observation_id"], panel["preview_digest"]) !=
                    (request_id, observation_id, preview_digest)):
                raise ValueError("remote recovery panel changed")
            expiry = datetime.fromisoformat(panel["expires_at"])
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                raise ValueError("remote recovery panel expired")
        old = conn.execute("SELECT * FROM broker_remote_cleanup WHERE request_id=?", (request_id,)).fetchone()
        if old:
            if old["observation_id"] != observation_id or old["preview_digest"] != preview_digest:
                raise ValueError("cleanup confirmation identity changed")
            return {"state": "already_recorded", "repair_verified": False, "reply_sent": False}
        action, code, actual = _candidate(conn, config, request_id, observation_id)
        if not isinstance(preview_digest, str) or preview_digest != actual:
            raise ValueError("cleanup preview stale")
        audit = conn.execute("SELECT * FROM broker_remote_observations WHERE observation_id=?", (observation_id,)).fetchone()
        conn.execute("INSERT INTO broker_remote_cleanup VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (request_id, observation_id, audit["snapshot_digest"], audit["result_json"],
                      action["grant_id"], action["request_digest"], action["plan_json"],
                      action["state"], action["updated_at"], code, actual, os.geteuid(), iso_now()))
    return {"state": "cleanup_recorded", "repair_verified": False, "reply_sent": False}
