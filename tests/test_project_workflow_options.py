"""Synthetic official metadata through real read normalization, no live Project."""
# ruff: noqa: F811 -- reuse isolated read fixtures
import copy

import pytest

from test_project_read_snapshot import Client, bound  # noqa: F401
from test_project_refresh import refresh_args  # noqa: F401

from k3_support import project_bug_controls as controls
from k3_support import project_bug_grants as grants
from k3_support import project_bug_sync as sync
from k3_support import project_bugs as bugs
from k3_support import project_field_writer_config as policy
from k3_support import project_workflow_options as workflow
from k3_support.project_read_client import ProjectReadError


class WorkflowClient(Client):
    def __init__(self):
        super().__init__()
        self.subject = "dedicated"
        self.requirements = {}
        self.before = lambda command: None
        self.workflow = {"state_key": "CLOSED", "state_name": "已关闭", "transition": [
            {"id": 21, "state_key": "open", "state_name": "重新打开", "confirm_form": None},
            {"id": 22, "state_key": "CLOSED", "state_name": "官方关闭选项", "confirm_form": None},
        ]}

    def read_page(self, command, params):
        self.before(command)
        if command in {"user.me", "workflow.list-state-transitions", "workflow.list-state-required"}:
            self.calls.append((command, copy.deepcopy(params)))
            payload = {"user_key": self.subject} if command == "user.me" else (
                self.workflow if command.endswith("transitions") else self.requirements)
            return {"host": self.host, "command": command, "payload": copy.deepcopy(payload)}
        return super().read_page(command, params)


@pytest.fixture
def workflow_context(conn, config, refresh_args):
    config.raw["project_integration"]["reader"]["sha256"] = next(iter(policy.ACCEPTED_TRANSITION_CLIENTS))
    config.raw["project_integration"]["field_writer"] = {
        "enabled": True, "user_key": "dedicated", "allowed_fields": ["progress"],
        "risk_policy": "forbid", "closing_status_ids": ["CLOSED"],
    }
    arguments = {k: v for k, v in refresh_args.items() if k != "request_id"}
    observed = sync.refresh(conn, Client(), **arguments, observation_id="workflow-snapshot")
    return arguments | {"snapshot_id": observed["snapshot_id"],
                        "expected_revision": bugs._bug(conn, arguments["bug_id"])["revision"]}


def test_choices_preserve_official_names_and_closure_class_without_mutation(conn, config, workflow_context):
    native = WorkflowClient()
    before = conn.total_changes
    result = workflow.read(conn, config, **workflow_context, client_factory=lambda _: native)
    assert conn.total_changes == before
    assert result["options"] == [
        {"transition_id": "21", "target_status_id": "open", "target_status_label": "重新打开",
         "action": "bug.transition", "required_complete": True, "missing_required": []},
        {"transition_id": "22", "target_status_id": "CLOSED", "target_status_label": "官方关闭选项",
         "action": "bug.close", "required_complete": True, "missing_required": []},
    ]
    assert result["source"] == "official_current_metadata"
    assert result["writer_user_key"] == "dedicated"
    assert all(command in {"user.me", "workitem.meta-fields", "workitem.get",
                           "workflow.list-state-transitions", "workflow.list-state-required"}
               for command, _ in native.calls)
    assert [p["user_key"] for c, p in native.calls if c.endswith("transitions")] == ["dedicated"]
    assert conn.execute("SELECT count(*) FROM project_bug_operations").fetchone()[0] == 0


def test_missing_or_unreadable_requirements_block_the_option(conn, config, workflow_context):
    for requirements in ({"form_items": [{"key": "root_cause"}]}, {"new_contract": True}):
        native = WorkflowClient()
        native.requirements = requirements
        result = workflow.read(conn, config, **workflow_context, client_factory=lambda _: native)
        assert not any(item["required_complete"] for item in result["options"])


def test_missing_required_keys_are_reported_without_writing(conn, config, workflow_context):
    native = WorkflowClient()
    native.requirements = {"form_items": [{"class": "field", "key": "field_6371d4"}]}
    before = conn.total_changes
    result = workflow.read(conn, config, **workflow_context, client_factory=lambda _: native)
    assert conn.total_changes == before
    assert all(item["missing_required"] == [{"key": "field_6371d4", "class": "field"}]
               for item in result["options"])


@pytest.mark.parametrize("change", ["local_revision", "remote_fields", "identity", "contract", "malformed"])
def test_stale_or_untrusted_choices_never_become_available(conn, config, workflow_context, change):
    native = WorkflowClient()
    args = dict(workflow_context)
    if change == "local_revision":
        args["expected_revision"] -= 1
    elif change == "remote_fields":
        for row in native.values:
            if "work_item_fields" in row:
                row["work_item_fields"][0]["value"] = "colleague edit"
    elif change == "identity":
        native.subject = "other-user"
    elif change == "contract":
        config.raw["project_integration"]["reader"]["sha256"] = "0" * 64
    else:
        native.workflow["transition"][0]["id"] = "unaccepted-string-id"
    with pytest.raises((bugs.BugConflict, ProjectReadError)):
        workflow.read(conn, config, **args, client_factory=lambda _: native)
    if change in {"local_revision", "contract"}:
        assert not native.calls


@pytest.mark.parametrize("change", ["revoke", "writer", "identity"])
def test_mid_read_authority_change_discards_results(conn, config, workflow_context, change):
    native = WorkflowClient()

    def mutate(command):
        if command != "workflow.list-state-transitions":
            return
        if change == "revoke":
            grants.revoke(conn, actor="owner", grant_id=workflow_context["grant_id"])
        elif change == "writer":
            config.raw["project_integration"]["field_writer"]["closing_status_ids"] = ["different"]
        else:
            native.subject = "changed-mid-read"

    native.before = mutate
    with pytest.raises((PermissionError, ProjectReadError)):
        workflow.read(conn, config, **workflow_context, client_factory=lambda _: native)


def test_public_control_binds_actor_and_does_not_accept_browser_identity(conn, config, workflow_context, monkeypatch):
    body = {k: v for k, v in workflow_context.items() if k != "actor"}
    monkeypatch.setattr(workflow, "MeegleReadClient", lambda **_: WorkflowClient())
    assert controls.execute(conn, config, action="workflow-options", payload=body)["options"]
    with pytest.raises(ValueError, match="exact request fields"):
        controls.execute(conn, config, action="workflow-options", payload=body | {"actor": "other"})
