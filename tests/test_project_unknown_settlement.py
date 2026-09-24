from __future__ import annotations

import copy
import json
import sqlite3
from datetime import timedelta

import pytest

from k3_support import project_bug_grants as grants
from k3_support import project_bug_operations as ops
from k3_support import project_bugs as bugs
from k3_support.config import Config, validate_config
from k3_support.db import migrate
from k3_support.ids import canonical_json
from k3_support.project_transport import ProjectReceipt, ProjectView
from k3_support.project_unknown_settlement import detail, projection, settle
from k3_support.store import create_case
from k3_support.timeutil import iso_now, utc_now


class UnknownAdapter:
    def __init__(self, view):
        self.view = view
        self.writes = []

    def preflight(self, destination):
        assert destination == self.view.destination
        return self.view

    def write(self, packet):
        self.writes.append(copy.deepcopy(packet))
        raise TimeoutError("write outcome is unknown after dispatch")

    def reconcile(self, packet):
        return ProjectReceipt(
            packet["operation_id"],
            packet["write_digest"],
            "unknown",
            True,
            "remote-operation-unknown",
            (),
        )


def _config(config):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["identity"]["control_operator_id"] = "owner"
    raw["project_integration"] = {"write_enabled": True}
    return Config(validate_config(raw), config.path)


def _setup_unknown_operation(conn, config):
    cfg = _config(config)
    case, _ = create_case(
        conn, title="Unknown settlement", case_type="bug", severity="P2", confidence=1
    )
    bug = bugs.bind(
        conn,
        case_id=case,
        host="project.feishu.cn",
        project_key="space",
        type_key="type",
        item_id="123",
        actor="owner",
    )
    snapshot = {
        "fields": {"progress": "old", "priority": "P2", "nullable": None},
        "status_id": "open",
        "closure": {"closed": False, "reason": None},
        "remote_version": "v1",
        "schema_digest": "schema1",
    }
    observation = bugs.observe(
        conn,
        bug_id=bug["bug_id"],
        observation_id="read-1",
        expected_sequence=0,
        payload=snapshot,
        observed_at=iso_now(),
    )
    scope = {
        "host": bug["host"],
        "project_key": "space",
        "type_key": "type",
        "bug_ids": [bug["bug_id"]],
        "actions": ["bug.fields", "bug.comment", "bug.transition", "bug.close"],
        "fields": ["progress", "priority", "nullable"],
        "transitions": ["to-test", "to-close"],
        "repositories": [],
        "devices": [],
    }
    grant = grants.issue(
        conn,
        actor="owner",
        request_id="grant",
        scope=scope,
        expires_at=(utc_now() + timedelta(hours=1)).isoformat(),
    )
    request = {
        "bug_id": bug["bug_id"],
        "snapshot_id": observation["snapshot_id"],
        "actor": "owner",
        "request_id": "request",
        "grant_id": grant["grant_id"],
        "expected_revision": 2,
        "action": "bug.fields",
        "change": {"fields": {"progress": "new"}},
    }
    adapter = UnknownAdapter(
        ProjectView(
            destination={
                k: bug[k] for k in ("host", "project_key", "type_key", "item_id")
            },
            snapshot=snapshot,
            observed_at=iso_now(),
            writable_fields=frozenset(scope["fields"]),
            transitions={
                "to-test": {
                    "target_status_id": "testing",
                    "closes": False,
                    "required_complete": True,
                },
                "to-close": {
                    "target_status_id": "closed",
                    "closes": True,
                    "required_complete": True,
                },
            },
            allowed_actions=frozenset(scope["actions"]),
            conditional_actions=frozenset(scope["actions"]),
            conditional_token="v1",
        )
    )
    operation = ops.prepare(conn, **request)
    result = ops.dispatch(conn, cfg, operation_id=operation["operation_id"], transport=adapter)
    assert result["state"] == "unknown"
    return cfg, bug, request, result, adapter


def _setup_prepared_operation(conn, config):
    cfg = _config(config)
    case, _ = create_case(
        conn, title="Unknown settlement", case_type="bug", severity="P2", confidence=1
    )
    bug = bugs.bind(
        conn,
        case_id=case,
        host="project.feishu.cn",
        project_key="space",
        type_key="type",
        item_id="123",
        actor="owner",
    )
    snapshot = {
        "fields": {"progress": "old", "priority": "P2", "nullable": None},
        "status_id": "open",
        "closure": {"closed": False, "reason": None},
        "remote_version": "v1",
        "schema_digest": "schema1",
    }
    observation = bugs.observe(
        conn,
        bug_id=bug["bug_id"],
        observation_id="read-1",
        expected_sequence=0,
        payload=snapshot,
        observed_at=iso_now(),
    )
    scope = {
        "host": bug["host"],
        "project_key": "space",
        "type_key": "type",
        "bug_ids": [bug["bug_id"]],
        "actions": ["bug.fields", "bug.comment", "bug.transition", "bug.close"],
        "fields": ["progress", "priority", "nullable"],
        "transitions": ["to-test", "to-close"],
        "repositories": [],
        "devices": [],
    }
    grant = grants.issue(
        conn,
        actor="owner",
        request_id="grant",
        scope=scope,
        expires_at=(utc_now() + timedelta(hours=1)).isoformat(),
    )
    request = {
        "bug_id": bug["bug_id"],
        "snapshot_id": observation["snapshot_id"],
        "actor": "owner",
        "request_id": "request",
        "grant_id": grant["grant_id"],
        "expected_revision": 2,
        "action": "bug.fields",
        "change": {"fields": {"progress": "new"}},
    }
    operation = ops.prepare(conn, **request)
    adapter = UnknownAdapter(
        ProjectView(
            destination={
                k: bug[k] for k in ("host", "project_key", "type_key", "item_id")
            },
            snapshot=snapshot,
            observed_at=iso_now(),
            writable_fields=frozenset(scope["fields"]),
            transitions={
                "to-test": {
                    "target_status_id": "testing",
                    "closes": False,
                    "required_complete": True,
                },
                "to-close": {
                    "target_status_id": "closed",
                    "closes": True,
                    "required_complete": True,
                },
            },
            allowed_actions=frozenset(scope["actions"]),
            conditional_actions=frozenset(scope["actions"]),
            conditional_token="v1",
        )
    )
    return cfg, bug, request, operation, adapter


def _settle(conn, config, verdict="confirmed_applied", evidence_text="operator note"):
    _, _, request, result, _ = _setup_unknown_operation(conn, config)
    settlement = settle(
        conn,
        operation_id=result["operation_id"],
        actor=request["actor"],
        verdict=verdict,
        evidence_text=evidence_text,
    )
    return request, result, settlement


def test_confirmed_applied_sets_operation_and_records_evidence(conn, config):
    _request, result, settlement = _settle(conn, config)
    op = ops._operation(conn, result["operation_id"])
    assert op["state"] == "confirmed"
    payload = json.loads(op["result_json"])
    assert payload["settled_by_human"] is True
    assert payload["settlement_id"] == settlement["settlement_id"]
    assert payload["evidence_digest"] == settlement["evidence_digest"]
    rows = conn.execute(
        "SELECT result_json FROM project_bug_operation_observations WHERE operation_id=?",
        (result["operation_id"],),
    ).fetchall()
    assert any(json.loads(row["result_json"]).get("settled_by_human") is True for row in rows)
    event = conn.execute(
        "SELECT * FROM project_bug_events WHERE bug_id=? AND kind='write_settled_by_human' ORDER BY created_at DESC LIMIT 1",
        (op["bug_id"],),
    ).fetchone()
    assert event is not None
    assert json.loads(event["detail_json"]) == {
        "operation_id": result["operation_id"],
        "settlement_id": settlement["settlement_id"],
        "verdict": "confirmed_applied",
    }
    assert projection(conn, result["operation_id"]) == settlement


def test_confirmed_not_applied_sets_rejected(conn, config):
    _, result, settlement = _settle(
        conn, config, verdict="confirmed_not_applied", evidence_text="not applied"
    )
    op = ops._operation(conn, result["operation_id"])
    assert op["state"] == "rejected"
    payload = json.loads(op["result_json"])
    assert payload["settled_by_human"] is True
    assert settlement["verdict"] == "confirmed_not_applied"


@pytest.mark.parametrize(
    "state",
    [
        "prepared",
        "dispatched",
        "confirmed",
        "rejected",
        "partial",
        "cancelled",
        "conflict",
        "satisfied",
    ],
)
def test_settling_non_unknown_states_is_rejected(conn, config, state):
    if state in {"prepared", "dispatched"}:
        _, _, request, operation, _ = _setup_prepared_operation(conn, config)
        operation_id = operation["operation_id"]
        if state == "dispatched":
            conn.execute(
                "UPDATE project_bug_operations SET state='dispatched' WHERE operation_id=?",
                (operation_id,),
            )
    elif state in {"cancelled", "conflict", "satisfied"}:
        _, _, request, operation, _ = _setup_prepared_operation(conn, config)
        operation_id = operation["operation_id"]
        conn.execute(
            "UPDATE project_bug_operations SET state=? WHERE operation_id=?",
            (state, operation_id),
        )
    else:
        _, _, request, result, _ = _setup_unknown_operation(conn, config)
        operation_id = result["operation_id"]
        conn.execute(
            "UPDATE project_bug_operations SET state=? WHERE operation_id=?",
            (state, operation_id),
        )
    with pytest.raises(bugs.BugConflict):
        settle(
            conn,
            operation_id=operation_id,
            actor=request["actor"],
            verdict="confirmed_applied",
            evidence_text="operator note",
        )


def test_wrong_actor_is_denied(conn, config):
    _, _, _request, result, _ = _setup_unknown_operation(conn, config)
    with pytest.raises(PermissionError, match="operation owner"):
        settle(
            conn,
            operation_id=result["operation_id"],
            actor="other",
            verdict="confirmed_applied",
            evidence_text="operator note",
        )


def test_missing_observations_block_human_settlement(conn, config):
    _config(config)
    case, _ = create_case(
        conn, title="Unknown settlement", case_type="bug", severity="P2", confidence=1
    )
    bug = bugs.bind(
        conn,
        case_id=case,
        host="project.feishu.cn",
        project_key="space",
        type_key="type",
        item_id="123",
        actor="owner",
    )
    snapshot = {
        "fields": {"progress": "old", "priority": "P2", "nullable": None},
        "status_id": "open",
        "closure": {"closed": False, "reason": None},
        "remote_version": "v1",
        "schema_digest": "schema1",
    }
    observation = bugs.observe(
        conn,
        bug_id=bug["bug_id"],
        observation_id="read-missing-1",
        expected_sequence=0,
        payload=snapshot,
        observed_at=iso_now(),
    )
    grant = grants.issue(
        conn,
        actor="owner",
        request_id="grant-missing",
        scope={
            "host": bug["host"],
            "project_key": "space",
            "type_key": "type",
            "bug_ids": [bug["bug_id"]],
            "actions": ["bug.fields"],
            "fields": ["progress"],
            "transitions": [],
            "repositories": [],
            "devices": [],
        },
        expires_at=(utc_now() + timedelta(hours=1)).isoformat(),
    )
    operation_id = "pbo_manual_unknown"
    conn.execute(
        """INSERT INTO project_bug_operations
        (operation_id,bug_id,snapshot_id,grant_id,actor,request_id,request_digest,
         bug_revision,case_version,action,change_json,state,write_json,write_digest,
         result_json,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            bug["bug_id"],
            observation["snapshot_id"],
            grant["grant_id"],
            "owner",
            "request-id",
            canonical_json({"request": "unknown"}),
            2,
            1,
            "bug.fields",
            canonical_json({"fields": {"progress": "new"}}),
            "unknown",
            None,
            None,
            None,
            iso_now(),
            iso_now(),
        ),
    )
    with pytest.raises(bugs.BugConflict, match="reconciliation must be attempted"):
        settle(
            conn,
            operation_id=operation_id,
            actor="owner",
            verdict="confirmed_applied",
            evidence_text="operator note",
        )


def test_replay_is_idempotent_and_evidence_must_match(conn, config):
    _, result, settlement = _settle(conn, config, evidence_text="same evidence")
    replay = settle(
        conn,
        operation_id=result["operation_id"],
        actor="owner",
        verdict="confirmed_applied",
        evidence_text="same evidence",
    )
    assert replay == settlement
    with pytest.raises(bugs.BugConflict, match="different evidence"):
        settle(
            conn,
            operation_id=result["operation_id"],
            actor="owner",
            verdict="confirmed_applied",
            evidence_text="different evidence",
        )


def test_settlement_rows_are_immutable(conn, config):
    _, _result, settlement = _settle(conn, config)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE project_unknown_settlements SET actor='other' WHERE settlement_id=?",
            (settlement["settlement_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM project_unknown_settlements WHERE settlement_id=?",
            (settlement["settlement_id"],),
        )


def test_migration_is_idempotent(conn):
    first = migrate(conn)
    second = migrate(conn)
    assert second == []
    assert first in ([], [132])


def test_detail_returns_evidence_only_for_owner(conn, config):
    _, result, settlement = _settle(conn, config, evidence_text="detail evidence")
    full = detail(
        conn,
        operation_id=result["operation_id"],
        actor="owner",
    )
    assert full["evidence_text"] == "detail evidence"
    assert full["settlement_id"] == settlement["settlement_id"]
    with pytest.raises(PermissionError):
        detail(conn, operation_id=result["operation_id"], actor="other")
