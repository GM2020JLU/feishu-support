"""Read current native workflow choices; never grant, prepare or dispatch writes.

Reuse the accepted transition/required-field contract and the snapshot collector.
The returned choices are a preview, not a permission token or server CAS. Normal
write preparation and dispatch must independently enforce their existing gates.
"""

import json

from . import project_bug_grants as grants
from . import project_bugs as bugs
from . import project_field_writer_config as policy
from .ids import canonical_json, digest
from .project_read_client import MeegleReadClient, ProjectReadError
from .project_read_snapshot import SnapshotReader
from .project_refresh import _fingerprint, _selection
from .project_write_transport import _required_details, _transitions


def read(conn, config, *, actor, bug_id, grant_id, snapshot_id,
         expected_revision, client_factory=None):
    bug = dict(bugs._bug(conn, bug_id))
    destination = {key: bug[key] for key in ("host", "project_key", "type_key", "item_id")}

    def context():
        current = bugs._bug(conn, bug_id)
        bugs._revision(current, expected_revision)
        grants.require_bug_read(conn, current, actor=actor, grant_id=grant_id)
        row = conn.execute(
            "SELECT snapshot_id,payload_json FROM project_bug_snapshots "
            "WHERE bug_id=? ORDER BY sequence DESC LIMIT 1", (bug_id,),
        ).fetchone()
        if row is None or row["snapshot_id"] != snapshot_id:
            raise bugs.BugConflict("workflow choices require the current Bug snapshot")
        reader = _selection(conn, config, current, actor)
        raw_writer = config.raw.get("project_integration", {}).get("field_writer")
        if raw_writer is None or not policy.accepted_transition(reader):
            raise ProjectReadError("workflow_contract_unavailable")
        writer = policy.validate(raw_writer)
        if not writer["closing_status_ids"]:
            raise ProjectReadError("workflow_closing_states_unconfigured")
        stamp = digest({"reader": _fingerprint(conn, config, reader), "writer": writer})
        return reader, writer, stamp, json.loads(row["payload_json"])

    reader, writer, stamp, base = context()

    def guard():
        if context()[2] != stamp:
            raise PermissionError("workflow authority changed during read")

    native = (client_factory or (
        lambda settings: MeegleReadClient(**{k: v for k, v in settings.items() if k != "enabled"})
    ))(reader)

    class GuardedReader:
        @property
        def host(self):
            return native.host

        def read_page(self, command, params):
            guard()
            answer = native.read_page(command, params)
            guard()
            return answer

    client = GuardedReader()

    def identity():
        answer = client.read_page("user.me", {})
        if (not isinstance(answer, dict) or answer.get("host") != destination["host"]
                or answer.get("command") != "user.me"
                or not isinstance(answer.get("payload"), dict)
                or answer["payload"].get("user_key") != writer["user_key"]):
            raise ProjectReadError("workflow_identity_changed")

    identity()
    observation = SnapshotReader(client, before_read=guard).collect(destination)
    if canonical_json(observation["snapshot"]) != canonical_json(base):
        raise bugs.BugConflict("remote Bug changed; refresh before choosing a workflow action")
    command = "workflow.list-state-transitions"
    answer = client.read_page(command, {
        "project_key": bug["project_key"], "work_item_id": bug["item_id"],
        "work_item_type": bug["type_key"], "user_key": writer["user_key"],
    })
    if (not isinstance(answer, dict) or answer.get("host") != bug["host"]
            or answer.get("command") != command):
        raise ProjectReadError("workflow_metadata_unavailable")
    payload = answer.get("payload")
    if (not isinstance(payload, dict)
            or set(payload) != {"state_key", "state_name", "transition"}
            or payload["state_key"] != base["status_id"]
            or not isinstance(payload["state_name"], str)
            or not isinstance(payload["transition"], list)):
        raise ProjectReadError("workflow_metadata_unavailable")
    requirements = {}

    def unfinished(state):
        if state not in requirements:
            requirements[state] = _required_details(client, guard, destination, state)
        details = requirements[state]
        return None if details is None else bool(details)

    normalized = _transitions(payload, destination, base,
                              frozenset(writer["closing_status_ids"]), unfinished)
    if payload["transition"] and not normalized:
        raise ProjectReadError("workflow_metadata_unavailable")
    options = []
    for row in payload["transition"]:
        item = normalized[str(row["id"])]
        options.append({"transition_id": str(row["id"]),
                        "target_status_id": item["target_status_id"],
                        "target_status_label": row["state_name"],
                        "action": "bug.close" if item["closes"] else "bug.transition",
                        "required_complete": item["required_complete"],
                        "missing_required": list(requirements.get(item["target_status_id"]) or [])})
    identity()
    guard()
    return {"snapshot_id": snapshot_id, "revision": expected_revision,
            "observed_at": observation["observed_at"],
            "current_status": {"id": payload["state_key"], "label": payload["state_name"]},
            "options": options, "writer_user_key": writer["user_key"],
            "source": "official_current_metadata"}
