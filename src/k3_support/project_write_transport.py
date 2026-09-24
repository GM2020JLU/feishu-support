"""Trusted conditional-write official CLI adapter, never a public model tool.

Mutation command and acknowledgement shapes follow the 2026-09-17 authorized
acceptance against the pinned native CLI (`workitem update` with an object
field array; `workflow transition-state` with only a transition id; update
acknowledged by ``{"mcp_result": ""}`` and transition by the bare JSON string
``"success"``). Any other response stays unknown for reconciliation. There is
no server CAS contract: the conditional token is a re-read window over the
item's own update marker under an explicitly configured risk policy, not an
atomicity claim. Custody states not_issued/precondition_failed
are recorded before any mutation call and are the only path to a terminal
rejected receipt; every other uncertainty stays unknown for read-only
reconciliation or recorded human settlement. Applied requires both the desired
remote state and a same-identity operation record within the attempt window,
which the single-consumer dispatch queue keeps unambiguous.
"""

import json
import re

from . import project_bug_operations as operations
from .db import transaction
from .ids import canonical_json, digest
from .project_history_reader import HistoryReader
from .project_read_client import MeegleReadClient, ProjectReadError
from .project_read_snapshot import SnapshotReader
from .project_transport import ProjectReceipt, ProjectView
from .timeutil import iso_now, parse_iso, utc_now

WRITE_ACTIONS = frozenset({"bug.fields", "bug.transition", "bug.close"})
_CLOCK_SKEW_MS = 120_000


def _token(observation):
    return digest(
        {
            "update_marker": observation["read_evidence"]["update_marker"],
            "status_id": observation["snapshot"]["status_id"],
            "schema_digest": observation["snapshot"]["schema_digest"],
            "observed_fields": _observed_snapshot(observation)["fields"],
            "version": 2,
        }
    )


def _flat_text(value):
    # Meegle rich text re-serializes paragraph breaks: a single "\n" written via
    # workitem update read back as "\n\n" (observed 2026-09-18, project.feishu.cn
    # issue 7117732933). Newline-run-insensitive equality keeps confirmation
    # deterministic without ever equating actual content changes.
    return re.sub(r"\n{2,}", "\n", value.replace("\r\n", "\n")).rstrip("\n")


def _field_confirmed(current, wanted, field_type=None):
    from .project_create_related import TYPES

    if field_type in TYPES:
        from .project_field_related import ids

        try:
            return ids(field_type, current, observed=True) == ids(field_type, wanted)
        except (ValueError, TypeError):
            return False
    if canonical_json(current) == canonical_json(wanted):
        return True
    if isinstance(current, dict) and isinstance(wanted, str):
        # Select values are written as the option id and read back as
        # {"label": ..., "value": option_id} (observed 2026-09-18, K3
        # field_7766c6). Only the id is compared; labels are display-only.
        return current.get("value") == wanted
    if (
        isinstance(current, list)
        and isinstance(wanted, list)
        and wanted
        and all(isinstance(item, dict) for item in current)
        and all(isinstance(item, str) for item in wanted)
    ):
        # User values are written as user-key lists and read back as member
        # objects [{"key": ..., "name": ..., "email": ...}].
        return sorted(item.get("key") for item in current) == sorted(wanted)
    return (
        type(current) is str
        and type(wanted) is str
        and _flat_text(current) == _flat_text(wanted)
    )


def _confirm_satisfied(entry, destination, fields):
    # Accepted metadata lists confirm requirements as field entries and role
    # entries such as role_<project_key>_<role_field>. A requirement counts as
    # complete only when the mapped logical field has an observed non-empty
    # value; unknown classes and unobservable roles fail closed.
    key = entry["key"]
    if entry.get("class") == "role":
        prefix = "role_" + destination["project_key"] + "_"
        if not key.startswith(prefix):
            return False
        # Accepted 2026-09-18 (K3 OPEN -> IN PROGRESS): snapshots expose the
        # current status operator as current_status_operator_role (label/value
        # pairs whose value is the full role key) plus current_status_operator
        # (its members). A confirm role matching that observed pairing with a
        # non-empty member list counts as complete.
        roles = fields.get("current_status_operator_role")
        members = fields.get("current_status_operator")
        if (
            isinstance(roles, list)
            and isinstance(members, list)
            and members
            and any(
                isinstance(pair, dict) and pair.get("value") == key for pair in roles
            )
        ):
            return True
        key = key[len(prefix) :]
    elif entry.get("class") != "field":
        return False
    return fields.get(key) not in (None, "", [], {})


def _required_details(client, before_read, destination, state_key):
    """Read authoritative missing requirements without treating omission as null.

    Accepted 2026-09-18 (K3): workflow.list-state-required with mode=unfinished
    answers {} when nothing is missing and {"form_items": [...]} otherwise.
    Returns a tuple of the official unfinished entries, or None when unreadable
    or off-contract. An empty tuple alone proves no requirement is missing.
    """
    before_read()
    envelope = client.read_page(
        "workflow.list-state-required",
        {
            "project_key": destination["project_key"],
            "work_item_id": destination["item_id"],
            "state_key": state_key,
            "mode": "unfinished",
        },
    )
    if (
        not isinstance(envelope, dict)
        or envelope.get("host") != destination["host"]
        or envelope.get("command") != "workflow.list-state-required"
        or not isinstance(envelope.get("payload"), dict)
    ):
        return None
    payload = envelope["payload"]
    if payload == {}:
        return ()
    items = payload.get("form_items")
    if (
        set(payload) != {"form_items"}
        or not isinstance(items, list)
        or len(items) > 50
        or not all(
            isinstance(item, dict) and isinstance(item.get("key"), str) and item["key"]
            for item in items
        )
    ):
        return None
    return tuple({"key": item["key"], "class": item.get("class") if isinstance(item.get("class"), str) else "unknown"}
                 for item in items)


def _unfinished_required(client, before_read, destination, state_key):
    """True if official metadata has unfinished items; None fails closed."""
    details = _required_details(client, before_read, destination, state_key)
    return None if details is None else bool(details)


def _transitions(payload, destination, snapshot, closing_status_ids, unfinished):
    """Strictly normalize the accepted list-state-transitions contract.

    Returns {} whenever any part of the payload deviates from the accepted
    shape or the reported current state disagrees with the snapshot, so a
    racing transition never advertises stale moves.
    """
    if (
        not isinstance(payload, dict)
        or set(payload) != {"state_key", "state_name", "transition"}
        or payload["state_key"] != snapshot["status_id"]
        or not isinstance(payload["state_name"], str)
        or not isinstance(payload["transition"], list)
        or len(payload["transition"]) > 50
    ):
        return {}
    fields = snapshot["fields"]
    result = {}
    for row in payload["transition"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"id", "state_key", "state_name", "confirm_form"}
            or type(row["id"]) is not int
            or not 0 < row["id"] < 2**63
            or not isinstance(row["state_key"], str)
            or not row["state_key"]
            or not isinstance(row["state_name"], str)
            or row["confirm_form"] is not None
            and not isinstance(row["confirm_form"], list)
        ):
            return {}
        confirm = row["confirm_form"] or []
        if len(confirm) > 50 or not all(
            isinstance(entry, dict)
            and set(entry) <= {"class", "key", "name"}
            and isinstance(entry.get("key"), str)
            and entry.get("key")
            for entry in confirm
        ):
            return {}
        key = str(row["id"])
        if key in result:
            return {}
        # Field-class confirm entries defer to the official required-field
        # answer; role-class entries stay locally checked against the fresh
        # snapshot. An unreadable required answer fails closed per target.
        role_ok = all(
            _confirm_satisfied(entry, destination, fields)
            for entry in confirm
            if entry.get("class") == "role"
        ) and all(
            entry.get("class") in {"field", "role"} for entry in confirm
        )
        result[key] = {
            "target_status_id": row["state_key"],
            "closes": row["state_key"] in closing_status_ids,
            "required_complete": role_ok
            and unfinished(row["state_key"]) is False,
        }
    return result


class WriteClient(MeegleReadClient):
    """Separate from the read-only client: exactly two mutation commands."""

    def _write_destination(self, destination):
        if (
            not isinstance(destination, dict)
            or set(destination) != {"host", "project_key", "type_key", "item_id"}
            or destination["host"] != self.host
            or not all(
                isinstance(v, str) and v and len(v) <= 2048
                for v in destination.values()
            )
            or not re.fullmatch(r"[1-9][0-9]*", destination["item_id"])
        ):
            raise ProjectReadError("invalid_write_destination")

    def update_fields(self, destination, fields):
        self._write_destination(destination)
        operations._change("bug.fields", {"fields": fields})
        # Accepted 2026-09-17: the gateway requires an array of objects whose
        # field_value is always a string; non-string values are canonically
        # JSON-encoded. No retry loop or caller flags.
        return self._success(
            [
                "workitem",
                "update",
                "--params",
                json.dumps(
                    {
                        "project_key": destination["project_key"],
                        "work_item_id": destination["item_id"],
                        "fields": [
                            {
                                "field_key": key,
                                "field_value": value
                                if isinstance(value, str)
                                else canonical_json(value),
                            }
                            for key, value in sorted(fields.items())
                        ],
                    },
                    ensure_ascii=False,
                ),
            ]
        )

    def transition_state(self, destination, change):
        self._write_destination(destination)
        operations._change("bug.transition", change)
        # Accepted 2026-09-17 as a JSON integer; on 2026-09-18 the same gateway
        # rejected integers ("transition_id must be string, got number") and
        # accepted strings. The gateway schema drifts, so identifiers now stay
        # strings end to end; the format check still rejects non-numeric input.
        if not re.fullmatch(r"[1-9][0-9]{0,17}", change["transition_id"]):
            raise ProjectReadError("invalid_transition_id")
        return self._success(
            [
                "workflow",
                "transition-state",
                "--params",
                json.dumps(
                    {
                        "project_key": destination["project_key"],
                        "work_item_id": destination["item_id"],
                        "transition_id": change["transition_id"],
                    },
                    ensure_ascii=False,
                ),
            ]
        )


def _ack(action, value):
    # Exactly the acknowledgement shapes observed in the 2026-09-17 authorized
    # acceptance; anything else stays unknown. Correlation still comes from the
    # readback state and same-identity history records, never from the ack.
    if action == "bug.fields":
        return value == {"mcp_result": ""}
    return value == "success"


def _observed_snapshot(observation):
    """Missing read values cannot establish an empty write baseline or receipt."""
    snapshot = observation["snapshot"]
    return {**snapshot, "fields": operations.observed_fields(snapshot, observation["read_evidence"])}


def _select_values_current(observation, values):
    evidence = observation["read_evidence"]
    for key, value in values.items():
        if evidence["field_types"].get(key) not in {"select", "tree-select"}:
            continue
        options = evidence["field_options"].get(key)
        if (not isinstance(value, str) or options is None
                or value not in {option["value"] for option in options}):
            return False
    return True


class WriteTransport:
    def __init__(
        self,
        conn,
        client,
        *,
        reader_digest,
        before_read,
        allowed_fields=frozenset(),
        risk_policy="forbid",
        closing_status_ids=frozenset(),
        update_contract_verified=False,
        transition_contract_verified=False,
        config=None,
    ):
        for flag in (update_contract_verified, transition_contract_verified):
            if type(flag) is not bool:
                raise TypeError("explicit contract verification flags required")
        if not isinstance(closing_status_ids, (frozenset, set)) or not all(
            isinstance(v, str) and v for v in closing_status_ids
        ):
            raise ValueError("closing status ids must be explicit strings")
        if risk_policy not in {"forbid", "recheck_window"}:
            raise ValueError("explicit write risk policy required")
        if not isinstance(reader_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", reader_digest
        ):
            raise ValueError("trusted reader fingerprint required")
        if not callable(before_read):
            raise TypeError("read authorization guard required")
        self.conn, self.client = conn, client
        self.config = config
        self.reader_digest, self.before_read = reader_digest, before_read
        self.allowed_fields = frozenset(allowed_fields)
        self.risk_policy = risk_policy
        self.closing_status_ids = frozenset(closing_status_ids)
        self.update_contract_verified = update_contract_verified
        self.transition_contract_verified = transition_contract_verified
        self.token = self.destination = self.observed_at = None
        self.evidence = None

    def preflight(self, destination):
        observation = SnapshotReader(self.client, before_read=self.before_read).collect(
            destination
        )
        self.before_read()
        evidence = observation["read_evidence"]
        self.evidence = evidence
        self.token = _token(observation)
        self.destination = dict(destination)
        self.observed_at = observation["observed_at"]
        actions = set()
        if self.update_contract_verified:
            actions.add("bug.fields")
        if self.transition_contract_verified:
            actions.update({"bug.transition", "bug.close"})
        conditional = (
            frozenset(actions) if self.risk_policy == "recheck_window" else frozenset()
        )
        observed_snapshot = _observed_snapshot(observation)
        observable = set(observed_snapshot["fields"]) - set(evidence["attachment_fields"])
        omitted = (set(evidence["unobserved_field_keys"])
                   | set(evidence["omitted_value_field_keys"]))
        fillable_required = self.allowed_fields & omitted - set(evidence["attachment_fields"])
        transitions = {}
        if self.transition_contract_verified:
            # Accepted 2026-09-17 transition-metadata contract; any deviation or
            # state race yields no advertised transitions rather than a guess.
            self.before_read()
            envelope = self.client.read_page(
                "workflow.list-state-transitions",
                {
                    "project_key": destination["project_key"],
                    "work_item_id": destination["item_id"],
                    "work_item_type": destination["type_key"],
                    "user_key": self.client.user_key,
                },
            )
            if (
                isinstance(envelope, dict)
                and envelope.get("host") == destination["host"]
                and envelope.get("command") == "workflow.list-state-transitions"
                and isinstance(envelope.get("payload"), dict)
            ):
                answers = {}

                def unfinished(state_key):
                    if state_key not in answers:
                        answers[state_key] = _unfinished_required(
                            self.client, self.before_read, destination, state_key
                        )
                    return answers[state_key]

                transitions = _transitions(
                    envelope["payload"],
                    destination,
                    observation["snapshot"],
                    self.closing_status_ids,
                    unfinished,
                )
        return ProjectView(
            destination=dict(destination),
            snapshot=observed_snapshot,
            observed_at=self.observed_at,
            writable_fields=self.allowed_fields & observable,
            fillable_required_fields=fillable_required,
            transitions=transitions,
            allowed_actions=frozenset(),
            conditional_actions=conditional,
            conditional_token=self.token if conditional else None,
            server_enforced_actions=frozenset(actions),
        )

    def _bound(self, packet):
        if not isinstance(packet, dict) or packet.get("action") not in WRITE_ACTIONS:
            raise ValueError("only dispatched field or transition writes are supported")
        op = operations._operation(self.conn, packet.get("operation_id"))
        if (
            op["state"] not in {"dispatched", "unknown"}
            or op["action"] != packet["action"]
            or op["write_json"] != canonical_json(packet)
            or op["write_digest"] != digest(packet)
        ):
            raise ValueError("immutable dispatched packet required")
        return op

    def write(self, packet):
        action = packet.get("action")
        if action == "bug.fields" and not self.update_contract_verified:
            raise PermissionError("native field update contract has not been accepted")
        if action in {"bug.transition", "bug.close"} and not (
            self.transition_contract_verified
        ):
            raise PermissionError("native transition contract has not been accepted")
        if self.risk_policy != "recheck_window":
            raise PermissionError("write dispatch requires an accepted risk policy")
        with transaction(self.conn):
            op = self._bound(packet)
            if (
                op["state"] != "dispatched"
                or self.token is None
                or packet["destination"] != self.destination
                or packet.get("conditional_token") != self.token
                or not 0
                <= (utc_now() - parse_iso(self.observed_at)).total_seconds()
                <= 60
            ):
                raise ValueError("fresh preflight required")
            if self.conn.execute(
                "SELECT 1 FROM project_write_attempts WHERE operation_id=?",
                (op["operation_id"],),
            ).fetchone():
                raise ValueError("write was already attempted; reconcile only")
            self.conn.execute(
                "INSERT INTO project_write_attempts(operation_id,write_digest,reader_digest,user_key,baseline_token,state,dispatched_at_ms,created_at,updated_at) VALUES(?,?,?,?,?,'reserved',?,?,?)",
                (
                    op["operation_id"],
                    op["write_digest"],
                    self.reader_digest,
                    self.client.user_key,
                    self.token,
                    int(utc_now().timestamp() * 1000),
                    iso_now(),
                    iso_now(),
                ),
            )
        issued = False
        state = response_digest = None
        try:
            change = packet["change"]
            if action == "bug.fields":
                from .project_field_related import validate_values

                # Target resolution may take time; complete it before the final
                # Bug snapshot check so a colleague's intervening edit conflicts.
                validate_values(
                    self.client, self.config, packet["destination"],
                    self.evidence, change["fields"], self.before_read,
                )
                required_missing = set(change.get("required_missing_fields", []))
                if required_missing:
                    target = change["required_target_status_id"]
                    details = _required_details(
                        self.client, self.before_read, packet["destination"], target
                    )
                    available = {
                        item["key"] for item in details or () if item["class"] == "field"
                    }
                    if details is None or not required_missing <= available:
                        raise ValueError("required field is no longer confirmed missing")
                    for key in required_missing:
                        field_type = self.evidence["field_types"].get(key)
                        value = change["fields"][key]
                        if field_type in {"text", "multi-text", "multi_text"}:
                            valid = isinstance(value, str) and bool(value.strip())
                        elif field_type in {"select", "tree-select"}:
                            options = self.evidence["field_options"].get(key, [])
                            valid = isinstance(value, str) and value in {
                                option["value"] for option in options
                            }
                        elif field_type == "user":
                            valid = value == self.client.user_key
                        else:
                            valid = False
                        if not valid:
                            raise ValueError("required field value is not accepted")
            check = SnapshotReader(self.client, before_read=self.before_read).collect(
                packet["destination"]
            )
            if action == "bug.fields" and change.get("required_missing_fields"):
                still_missing = set(check["read_evidence"]["unobserved_field_keys"]) | set(
                    check["read_evidence"]["omitted_value_field_keys"]
                )
                if not set(change["required_missing_fields"]) <= still_missing:
                    state = "precondition_failed"
            if (_token(check) != self.token or action == "bug.fields"
                    and not _select_values_current(check, packet["change"]["fields"])):
                state = "precondition_failed"
            if state != "precondition_failed":
                self.before_read()
                issued = True
                response = (
                    self.client.update_fields(packet["destination"], change["fields"])
                    if action == "bug.fields"
                    else self.client.transition_state(packet["destination"], change)
                )
                state = "acknowledged" if _ack(action, response) else "unknown"
                response_digest = digest(response)
        except Exception:  # noqa: BLE001 -- never save raw provider errors or replay writes
            state = "unknown" if issued else "not_issued"
            response_digest = None
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE project_write_attempts SET state=?,response_digest=?,settled_at_ms=?,updated_at=? WHERE operation_id=? AND state='reserved'",
                (
                    state,
                    response_digest,
                    int(utc_now().timestamp() * 1000),
                    iso_now(),
                    op["operation_id"],
                ),
            )

    def reconcile(self, packet):
        self._bound(packet)
        signature = digest(packet)
        unknown = ProjectReceipt(packet["operation_id"], signature, "unknown", False)
        row = self.conn.execute(
            "SELECT * FROM project_write_attempts WHERE operation_id=?",
            (packet["operation_id"],),
        ).fetchone()
        if (
            row is None
            or row["write_digest"] != signature
            or row["reader_digest"] != self.reader_digest
        ):
            return unknown
        if row["state"] in {"precondition_failed", "not_issued"}:
            return ProjectReceipt(
                packet["operation_id"],
                signature,
                "rejected",
                True,
                "write-attempt:" + row["state"],
            )
        # An opaque same-user history entry cannot correlate an uncertain call.
        # Only a validated success acknowledgement plus independent readback can
        # establish this attempt. Unknown/reserved calls need recorded human
        # reconciliation until the provider supplies reliable request correlation.
        if row["state"] != "acknowledged":
            return unknown
        observation = SnapshotReader(self.client, before_read=self.before_read).collect(
            packet["destination"]
        )
        snapshot = _observed_snapshot(observation)
        change = packet["change"]
        if packet["action"] == "bug.fields":
            current = snapshot["fields"]
            if any(
                key not in current or not _field_confirmed(
                    current[key], value,
                    observation["read_evidence"]["field_types"].get(key),
                )
                for key, value in change["fields"].items()
            ):
                return unknown
        elif snapshot["status_id"] != change["target_status_id"]:
            return unknown
        end = int(utc_now().timestamp() * 1000)
        history = HistoryReader(self.client, before_read=self.before_read).collect(
            packet["destination"], end=end
        )
        self.before_read()
        window_start = row["dispatched_at_ms"] - _CLOCK_SKEW_MS
        window_end = (row["settled_at_ms"] or end) + _CLOCK_SKEW_MS
        matches = [
            item
            for item in history["items"]
            if item["operator_key"] == row["user_key"]
            and window_start <= item["operation_time_ms"] <= window_end
        ]
        if not matches:
            return unknown
        return ProjectReceipt(
            packet["operation_id"],
            signature,
            "applied",
            True,
            "project-op-record:" + matches[-1]["record_digest"],
            tuple(sorted(change.get("fields", {}))),
        )
