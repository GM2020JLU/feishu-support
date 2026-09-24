"""Fault injection at the trusted adapter boundary; never calls live Project."""

import copy
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from k3_support import project_bug_grants as grants
from k3_support import project_bug_operations as ops
from k3_support import project_bugs as bugs
from k3_support.config import Config, ConfigError, validate_config
from k3_support.db import connect
from k3_support.ids import digest
from k3_support.project_transport import ProjectReceipt, ProjectView
from k3_support.store import create_case
from k3_support.timeutil import iso_now, observed_clock, utc_now


@pytest.mark.parametrize(
    "field",
    ["work_item_status", "current_status_operator", "current_status_operator_role"],
)
def test_reserved_workflow_fields_cannot_be_written_through_bug_fields(field):
    with pytest.raises(ValueError, match="reserved workflow field"):
        ops._change("bug.fields", {"fields": {field: "value"}})


def test_prepare_rejects_unobserved_placeholder_in_persisted_source(conn, setup):
    _, request, adapter = setup
    scope = {"host":"project.feishu.cn","project_key":"space","type_key":"type",
             "bug_ids":[request["bug_id"]],"actions":["bug.read"],"fields":[],
             "transitions":[],"repositories":[],"devices":[]}
    grant = grants.issue(conn, actor="owner", request_id="read-placeholder",
                         scope=scope, expires_at=(utc_now()+timedelta(hours=1)).isoformat())
    observation = bugs.observe(conn, bug_id=request["bug_id"], observation_id="placeholder-read",
                               expected_sequence=1, payload=adapter.view.snapshot, observed_at=iso_now(),
                               read_source={"actor":"owner","grant_id":grant["grant_id"],
                                            "evidence":{"unobserved_field_keys":["nullable"]}})
    with pytest.raises(ValueError, match="absent from the source snapshot"):
        ops.prepare(conn, **(request | {"snapshot_id":observation["snapshot_id"],
                     "expected_revision":bugs.detail(conn,request["bug_id"])["revision"],
                     "change":{"fields":{"nullable":"new"}}}))
    assert conn.execute("SELECT count(*) FROM project_bug_operations").fetchone()[0] == 0


def test_preview_distinguishes_unobserved_current_value_from_explicit_null(conn, setup):
    _, request, adapter = setup
    operation = ops.prepare(conn, **(request | {"change":{"fields":{"nullable":"new"}}}))
    current = copy.deepcopy(adapter.view.snapshot)
    assert ops.preview(conn,operation["operation_id"],current)["differences"][0]["state"] == "change"
    current["read_evidence"] = {"unobserved_field_keys":["nullable"]}
    diff = ops.preview(conn,operation["operation_id"],current)["differences"][0]
    assert diff["state"] == "unavailable" and diff["base_present"] and not diff["current_present"]


class Adapter:
    """Stateful synthetic provider with independently controllable write/receipt."""

    def __init__(self, view):
        self.view = view
        self.writes = []
        self.reads = 0
        self.result = "applied"
        self.terminal = True
        self.applied_fields = None
        self.timeout_after_apply = False
        self.before_return = lambda: None
        self.receipt_operation = None

    def preflight(self, destination):
        assert destination == self.view.destination
        self.before_return()
        return self.view

    def write(self, packet):
        self.writes.append(copy.deepcopy(packet))
        if self.timeout_after_apply:
            raise TimeoutError("sensitive-provider-message-must-not-be-recorded")

    def reconcile(self, packet):
        self.reads += 1
        fields = (
            tuple(packet["change"].get("fields", {}))
            if self.applied_fields is None
            else self.applied_fields
        )
        return ProjectReceipt(
            self.receipt_operation or packet["operation_id"],
            digest(packet),
            self.result,
            self.terminal,
            "remote-operation-123",
            fields,
        )


@pytest.fixture
def setup(conn, config):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["identity"]["control_operator_id"] = "owner"
    raw["project_integration"] = {"write_enabled": True}
    cfg = Config(validate_config(raw), config.path)
    case, _ = create_case(
        conn, title="Synthetic write", case_type="bug", severity="P2", confidence=1
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
    adapter = Adapter(
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
    return cfg, request, adapter


def dispatch(conn, setup, request=None):
    cfg, original, adapter = setup
    op = ops.prepare(conn, **(request or original))
    return ops.dispatch(conn, cfg, operation_id=op["operation_id"], transport=adapter)


def test_exact_replay_across_entry_points_never_redispatches(conn, setup):
    _, request, adapter = setup
    result = dispatch(conn, setup)
    assert result["state"] == "confirmed"
    assert ops.prepare(conn, **request)["operation_id"] == result["operation_id"]
    with pytest.raises(bugs.BugConflict, match="never dispatch"):
        ops.dispatch(
            conn, setup[0], operation_id=result["operation_id"], transport=adapter
        )
    assert len(adapter.writes) == 1
    with pytest.raises(bugs.BugConflict, match="reused"):
        ops.prepare(
            conn, **{**request, "change": {"fields": {"progress": "different"}}}
        )


def test_unknown_survives_new_database_connection_and_revoked_grant(
    conn, setup, tmp_path
):
    cfg, request, adapter = setup
    adapter.timeout_after_apply = True
    adapter.result = "unknown"
    result = dispatch(conn, setup)
    assert result["state"] == "unknown"
    grants.revoke(conn, grant_id=request["grant_id"], actor="owner")
    second = connect(tmp_path / "support.db")
    try:
        with pytest.raises(bugs.BugConflict):
            ops.dispatch(
                second, cfg, operation_id=result["operation_id"], transport=adapter
            )
        with pytest.raises(bugs.BugConflict):
            ops.cancel(
                second,
                operation_id=result["operation_id"],
                actor="owner",
                expected_digest=result["request_digest"],
            )
        adapter.result = "applied"
        recovered = ops.reconcile(
            second, operation_id=result["operation_id"], transport=adapter
        )
        assert recovered["state"] == "confirmed"
        assert len(adapter.writes) == 1
        assert "sensitive-provider-message" not in json.dumps(recovered)
    finally:
        second.close()


def test_unknown_blocks_new_operation_with_new_request_id(conn, setup):
    _, request, adapter = setup
    adapter.result = "unknown"
    dispatch(conn, setup)
    with pytest.raises(bugs.BugConflict, match="unsettled"):
        ops.prepare(conn, **{**request, "request_id": "new-id"})


@pytest.mark.parametrize(
    "fault",
    [
        "field_conflict",
        "field_missing",
        "schema_change",
        "no_cas",
        "no_permission",
        "different_destination",
        "stale",
    ],
)
def test_remote_changes_block_before_side_effect(conn, setup, fault):
    _, _, adapter = setup
    view = adapter.view
    snapshot = copy.deepcopy(view.snapshot)
    if fault == "field_conflict":
        snapshot["fields"]["progress"] = "colleague"
    elif fault == "field_missing":
        del snapshot["fields"]["progress"]
    elif fault == "schema_change":
        snapshot["schema_digest"] = "schema2"
    elif fault == "no_cas":
        view = replace(view, conditional_actions=frozenset())
    elif fault == "no_permission":
        view = replace(view, allowed_actions=frozenset())
    elif fault == "different_destination":
        view = replace(view, destination={**view.destination, "item_id": "999"})
        adapter.preflight = lambda _: view
    elif fault == "stale":
        view = replace(view, observed_at=(utc_now() - timedelta(minutes=5)).isoformat())
    adapter.view = replace(view, snapshot=snapshot)
    with pytest.raises((ValueError, PermissionError)):
        dispatch(conn, setup)
    assert adapter.writes == []


def test_unrelated_colleague_field_is_never_written(conn, setup):
    _, _, adapter = setup
    adapter.view.snapshot["fields"]["priority"] = "P0"
    assert dispatch(conn, setup)["state"] == "confirmed"
    assert adapter.writes[0]["change"] == {"fields": {"progress": "new"}}


@pytest.mark.parametrize("fault", ["revoke", "pause", "mode", "expiry"])
def test_local_authority_is_rechecked_after_slow_preflight(conn, setup, fault):
    _, request, adapter = setup

    def during_read():
        if fault == "revoke":
            grants.revoke(conn, grant_id=request["grant_id"], actor="owner")
        elif fault == "pause":
            conn.execute("UPDATE cases SET state='paused',version=version+1")
        elif fault == "mode":
            setup[0].raw["mode"] = "shadow"
        else:
            # Exercise expiry without sleeping and without mutating immutable scope.
            pass

    adapter.before_return = during_read
    if fault == "expiry":
        old = adapter.preflight

        def expired_read(destination):
            result = old(destination)
            grant = conn.execute(
                "SELECT expires_at FROM project_bug_grants"
            ).fetchone()[0]
            from k3_support.timeutil import parse_iso

            clock = observed_clock(parse_iso(grant))
            clock.__enter__()
            adapter.clock = clock
            return result

        adapter.preflight = expired_read
    try:
        with pytest.raises((ValueError, PermissionError)):
            dispatch(conn, setup)
    finally:
        if hasattr(adapter, "clock"):
            adapter.clock.__exit__(None, None, None)
    assert adapter.writes == []


def test_partial_receipt_releases_only_after_terminal_proof(conn, setup):
    _, request, adapter = setup
    request = {**request, "change": {"fields": {"progress": "new", "priority": "P1"}}}
    adapter.result, adapter.applied_fields, adapter.terminal = (
        "partial",
        ("progress",),
        False,
    )
    result = dispatch(conn, setup, request)
    assert result["state"] == "unknown"
    adapter.terminal = True
    result = ops.reconcile(conn, operation_id=result["operation_id"], transport=adapter)
    assert result["state"] == "partial"
    assert json.loads(result["result_json"])["applied_fields"] == ["progress"]
    assert len(adapter.writes) == 1


@pytest.mark.parametrize("fault", ["wrong_operation", "wrong_subset", "missing_subset"])
def test_invalid_receipt_keeps_uncertainty_barrier(conn, setup, fault):
    _, _, adapter = setup
    if fault == "wrong_operation":
        adapter.receipt_operation = "another-operation"
    elif fault == "wrong_subset":
        adapter.applied_fields = ("unrequested",)
    else:
        adapter.applied_fields = ()
    assert dispatch(conn, setup)["state"] == "unknown"


def test_comments_do_not_require_cas_but_still_require_correlation(conn, setup):
    _, request, adapter = setup
    adapter.view = replace(
        adapter.view, conditional_actions=frozenset(), conditional_token=None
    )
    request = {
        **request,
        "action": "bug.comment",
        "change": {"text": "Verified build only; device test pending"},
    }
    assert dispatch(conn, setup, request)["state"] == "confirmed"


@pytest.mark.parametrize("action", ["bug.transition", "bug.close"])
def test_closure_cannot_bypass_evidence_gate(conn, setup, action):
    _, request, adapter = setup
    request = {
        **request,
        "action": action,
        "change": {"transition_id": "to-close", "target_status_id": "closed"},
    }
    # A closing transition must be requested as bug.close; bug.close itself is
    # blocked until a digest-bound approval over passed verification is consumed.
    expected = bugs.BugConflict if action == "bug.transition" else PermissionError
    with pytest.raises(expected, match="closure"):
        dispatch(conn, setup, request)
    assert not adapter.writes


def test_transition_validates_real_target_and_required_fields(conn, setup):
    _, request, adapter = setup
    request = {
        **request,
        "action": "bug.transition",
        "change": {"transition_id": "to-test", "target_status_id": "testing"},
    }
    adapter.view.transitions["to-test"]["required_complete"] = False
    with pytest.raises(bugs.BugConflict):
        dispatch(conn, setup, request)
    adapter.view.transitions["to-test"]["required_complete"] = True
    assert dispatch(conn, setup, request)["state"] == "confirmed"


def test_cancel_preview_does_not_call_provider(conn, setup):
    _, request, adapter = setup
    operation = ops.prepare(conn, **request)
    with pytest.raises(PermissionError):
        ops.cancel(
            conn,
            operation_id=operation["operation_id"],
            actor="other",
            expected_digest=operation["request_digest"],
        )
    result = ops.cancel(
        conn,
        operation_id=operation["operation_id"],
        actor="owner",
        expected_digest=operation["request_digest"],
    )
    assert result["state"] == "cancelled"
    assert adapter.writes == [] and adapter.reads == 0


def test_missing_and_null_are_distinct_and_booleans_are_not_numbers():
    assert ops.field_diff({"a": None}, {}, {"a": 1})[0]["state"] == "unavailable"
    assert ops.field_diff({"a": None}, {"a": None}, {"a": 1})[0]["state"] == "change"
    assert ops.field_diff({"a": 0}, {"a": False}, {"a": 1})[0]["state"] == "conflict"


def test_opt_in_config_is_strict_and_default_disabled(config):
    assert "project_integration" not in validate_config(config.raw)
    for setting in (
        None,
        True,
        {"write_enabled": 1},
        {"write_enabled": True},
        {"write_enabled": False, "token": "not-allowed"},
    ):
        with pytest.raises(ConfigError):
            validate_config({**config.raw, "project_integration": setting})


def test_matching_values_are_observed_without_repeating_write(conn, setup):
    _, _, adapter = setup
    adapter.view.snapshot["fields"]["progress"] = "new"
    result = dispatch(conn, setup)
    assert result["state"] == "satisfied"
    assert json.loads(result["result_json"])["write_performed"] is False
    assert adapter.writes == []


def test_process_death_after_dispatch_is_reconciled_not_replayed(conn, setup):
    class ProcessDeath(BaseException):
        pass

    _, request, adapter = setup

    def crash(packet):
        adapter.writes.append(copy.deepcopy(packet))
        raise ProcessDeath()

    adapter.write = crash
    with pytest.raises(ProcessDeath):
        dispatch(conn, setup)
    operation = ops.prepare(conn, **request)
    assert operation["state"] == "dispatched"
    recovered = ops.reconcile(
        conn, operation_id=operation["operation_id"], transport=adapter
    )
    assert recovered["state"] == "confirmed"
    assert len(adapter.writes) == 1


def test_preflight_failure_is_sanitized_and_does_not_dispatch(conn, setup):
    _, _, adapter = setup

    def failed(destination):
        raise OSError("secret-provider-error")

    adapter.preflight = failed
    with pytest.raises(RuntimeError, match="no write was dispatched") as error:
        dispatch(conn, setup)
    assert "secret-provider-error" not in str(error.value)
    assert not adapter.writes


def test_competing_dispatchers_issue_only_one_write(conn, setup, tmp_path):
    cfg, request, first = setup
    operation = ops.prepare(conn, **request)
    second = Adapter(first.view)
    other = connect(tmp_path / "support.db")
    try:
        first.before_return = lambda: ops.dispatch(
            other, cfg, operation_id=operation["operation_id"], transport=second
        )
        with pytest.raises(bugs.BugConflict, match="changed while"):
            ops.dispatch(
                conn, cfg, operation_id=operation["operation_id"], transport=first
            )
        assert not first.writes
        assert len(second.writes) == 1
        assert ops.prepare(conn, **request)["state"] == "confirmed"
    finally:
        other.close()


def test_conditional_provider_rejection_preserves_colleague_edit(conn, setup):
    _, _, adapter = setup
    applied = []

    def concurrent_write(packet):
        # Provider revision changed AFTER preflight; only server-side CAS closes
        # this race. This fixture tests the adapter contract, not official support.
        adapter.writes.append(packet)
        if packet["conditional_token"] != "v2":
            adapter.result = "rejected"
            adapter.applied_fields = ()
        else:
            applied.append(packet)

    adapter.write = concurrent_write
    assert dispatch(conn, setup)["state"] == "rejected"
    assert not applied
