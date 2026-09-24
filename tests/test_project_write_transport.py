# ruff: noqa: F811 -- shared isolated fixtures
"""Write transport custody, recheck window and history correlation; no live Project."""

import copy
import json

import pytest
from test_project_activity import record
from test_project_bug_operations import setup  # noqa: F401
from test_project_read_client import binary  # noqa: F401
from test_project_read_snapshot import Client, responses

from k3_support import project_bug_operations as operations
from k3_support import project_bugs as bugs
from k3_support.project_bugs import BugConflict
from k3_support.project_read_client import ProjectReadError
from k3_support.project_read_snapshot import SnapshotReader
from k3_support.project_unknown_settlement import settle
from k3_support.project_write_transport import (
    WriteClient,
    WriteTransport,
    _ack,
    _transitions,
)
from k3_support.timeutil import iso_now, utc_now

FINGERPRINT = "b" * 64


def snapshot_pages(progress="old", marker="opaque-marker"):
    values = responses()
    for index in (1, 3):
        attribute = values[index]["work_item_attribute"]
        attribute["work_item_type"]["key"] = "type"
        attribute["update_time"] = marker
        values[index]["work_item_fields"] = [
            {"key": "progress", "name": "Progress", "value": progress}
        ]
    return values


def op_record(operator="writer-user", time_ms=None):
    value = record(1)
    value.update(
        operator=operator,
        work_item_type_key="type",
        operation_time=time_ms
        if time_ms is not None
        else int(utc_now().timestamp() * 1000),
    )
    return value


def history(*records):
    page = {
        "op_records": list(records),
        "has_more": False,
        "start_from": "",
        "total": 0,
    }
    return [page, copy.deepcopy(page)]


def transition_payload(*rows, state_key="CLOSED"):
    return {
        "state_key": state_key,
        "state_name": "Verified",
        "transition": list(rows),
    }


def transition_row(
    transition_id=101,
    *,
    state_key="TESTING",
    state_name="Testing",
    confirm_form=None,
):
    return {
        "id": transition_id,
        "state_key": state_key,
        "state_name": state_name,
        "confirm_form": confirm_form,
    }


def transition_page(*rows, state_key="CLOSED"):
    return transition_payload(*rows, state_key=state_key)


class WriteFake(Client):
    user_key = "writer-user"

    def __init__(self, values, response=None):
        super().__init__(values)
        self.updates = []
        self.transitions = []
        self.response = {"mcp_result": ""} if response is None else response

    def update_fields(self, destination, fields):
        self.updates.append((copy.deepcopy(destination), copy.deepcopy(fields)))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    def transition_state(self, destination, change):
        self.transitions.append((copy.deepcopy(destination), copy.deepcopy(change)))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def prepare(conn, setup, *, action="bug.fields", change=None, progress="old"):
    cfg, request, adapter = setup
    destination = adapter.view.destination
    snapshot = SnapshotReader(Client(snapshot_pages(progress=progress))).collect(
        destination
    )["snapshot"]
    observation = bugs.observe(
        conn,
        bug_id=request["bug_id"],
        observation_id="fresh",
        expected_sequence=1,
        payload=snapshot,
        observed_at=iso_now(),
    )
    request = request | {
        "action": action,
        "change": change or {"fields": {"progress": "new"}},
        "snapshot_id": observation["snapshot_id"],
        "expected_revision": bugs.detail(conn, request["bug_id"])["revision"],
    }
    return cfg, operations.prepare(conn, **request)


def make_transport(conn, client, **kwargs):
    options = {
        "reader_digest": FINGERPRINT,
        "before_read": lambda: None,
        "allowed_fields": frozenset({"progress"}),
        "risk_policy": "recheck_window",
        "update_contract_verified": True,
        "transition_contract_verified": False,
    } | kwargs
    return WriteTransport(conn, client, **options)


def custody(conn):
    return conn.execute("SELECT * FROM project_write_attempts").fetchall()


@pytest.mark.parametrize(
    "options,match",
    [
        (
            {
                "update_contract_verified": False,
                "transition_contract_verified": False,
                "risk_policy": "forbid",
            },
            "does not permit write",
        ),
        ({"risk_policy": "forbid"}, "conditional-write contract"),
    ],
)
def test_unverified_contract_or_forbid_policy_refuses_before_any_write(
    conn, setup, options, match
):
    cfg, op = prepare(conn, setup)
    fake = WriteFake(snapshot_pages() + [{}])
    with pytest.raises(PermissionError, match=match):
        operations.dispatch(
            conn,
            cfg,
            operation_id=op["operation_id"],
            transport=make_transport(conn, fake, **options),
        )
    assert fake.updates == [] and custody(conn) == []
    assert operations._operation(conn, op["operation_id"])["state"] == "prepared"


def test_matching_remote_state_settles_satisfied_without_any_mutation(conn, setup):
    cfg, op = prepare(conn, setup, progress="new")
    fake = WriteFake(snapshot_pages(progress="new"))
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=make_transport(conn, fake)
    )
    assert result["state"] == "satisfied"
    assert json.loads(result["result_json"])["write_performed"] is False
    assert fake.updates == [] and custody(conn) == []


def missing_progress_pages():
    pages = snapshot_pages()
    for index in (1, 3):
        del pages[index]["work_item_fields"][0]["value"]
    return pages


def prepare_required_fill(conn, setup):
    cfg, request, adapter = setup
    from datetime import timedelta

    from k3_support import project_bug_grants as grants

    from k3_support.timeutil import utc_now

    read_scope = {"host": adapter.view.destination["host"], "project_key": "space",
                  "type_key": "type", "bug_ids": [request["bug_id"]],
                  "actions": ["bug.read"], "fields": [], "transitions": [],
                  "repositories": [], "devices": []}
    read_grant = grants.issue(
        conn, actor="owner", request_id="required-fill-read", scope=read_scope,
        expires_at=(utc_now() + timedelta(hours=1)).isoformat(),
    )
    observation = SnapshotReader(Client(missing_progress_pages())).collect(
        adapter.view.destination
    )
    saved = bugs.observe(
        conn, bug_id=request["bug_id"], observation_id="missing-required-read",
        expected_sequence=1, payload=observation["snapshot"], observed_at=iso_now(),
        read_source={"actor": "owner", "grant_id": read_grant["grant_id"],
                     "evidence": observation["read_evidence"]},
    )
    op = operations.prepare(conn, **(request | {
        "snapshot_id": saved["snapshot_id"],
        "expected_revision": bugs.detail(conn, request["bug_id"])["revision"],
        "request_id": "fill-required-progress",
        "change": {"fields": {"progress": "new"},
                   "required_target_status_id": "RESOLVED",
                   "required_missing_fields": ["progress"]},
    }))
    return cfg, op


@pytest.mark.parametrize("required", [True, False])
def test_authoritative_missing_requirement_controls_fill_write(conn, setup, required):
    cfg, op = prepare_required_fill(conn, setup)
    requirement = {"form_items": [{"class": "field", "key": "progress"}]} if required else {}
    pages = missing_progress_pages() + [requirement]
    if required:
        pages += missing_progress_pages() + snapshot_pages(progress="new") + history(op_record())
    fake = WriteFake(pages)
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"],
        transport=make_transport(conn, fake),
    )
    assert result["state"] == ("confirmed" if required else "rejected")
    assert len(fake.updates) == (1 if required else 0)


def test_required_fill_stops_if_field_appears_before_issue(conn, setup):
    cfg, op = prepare_required_fill(conn, setup)
    fake = WriteFake(
        missing_progress_pages()
        + [{"form_items": [{"class": "field", "key": "progress"}]}]
        + snapshot_pages(progress="colleague value")
    )
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"],
        transport=make_transport(conn, fake),
    )
    assert result["state"] == "rejected"
    assert not fake.updates
    assert custody(conn)[0]["state"] == "precondition_failed"


@pytest.mark.parametrize("target", [None, "new"])
def test_unobserved_field_cannot_be_written_or_satisfied_as_null(conn, setup, target):
    cfg, op = prepare(conn, setup, change={"fields": {"progress": target}})
    fake = WriteFake(missing_progress_pages())
    with pytest.raises(PermissionError, match="ordinary writable field"):
        operations.dispatch(conn, cfg, operation_id=op["operation_id"],
                            transport=make_transport(conn, fake))
    assert fake.updates == [] and custody(conn) == []
    assert operations._operation(conn, op["operation_id"])["state"] == "prepared"


def test_acknowledged_clear_with_missing_readback_stays_unknown(conn, setup):
    cfg, op = prepare(conn, setup, change={"fields": {"progress": None}})
    fake = WriteFake(snapshot_pages() + snapshot_pages() + missing_progress_pages())
    result = operations.dispatch(conn, cfg, operation_id=op["operation_id"],
                                 transport=make_transport(conn, fake))
    assert result["state"] == "unknown"
    assert len(fake.updates) == 1


def test_explicit_null_is_a_valid_observed_backfill_baseline(conn, setup):
    cfg, op = prepare(conn, setup, progress=None)
    fake = WriteFake(snapshot_pages(progress=None) + snapshot_pages(progress=None)
                     + snapshot_pages(progress="new") + history(op_record()))
    result = operations.dispatch(conn, cfg, operation_id=op["operation_id"],
                                 transport=make_transport(conn, fake))
    assert result["state"] == "confirmed"
    assert len(fake.updates) == 1


@pytest.mark.parametrize("target", ["new", "removed-option"])
def test_single_select_write_checks_current_official_options(conn, setup, monkeypatch, target):
    original = snapshot_pages
    def select_pages(progress="old", marker="opaque-marker"):
        pages = original(progress, marker)
        for index in (0, 2):
            row = pages[index]["list"][1]
            row["field_type"] = "select"
            row["option"] = [{"option_id": "old", "option_name": "Old"},
                             {"option_id": "new", "option_name": "New"}]
        return pages
    monkeypatch.setattr(__import__(__name__), "snapshot_pages", select_pages)
    cfg, op = prepare(conn, setup, change={"fields": {"progress": target}})
    fake = WriteFake(select_pages() + select_pages() +
                     (select_pages(progress={"value":"new", "label":"New"}) + history(op_record())
                      if target == "new" else []))
    result = operations.dispatch(conn, cfg, operation_id=op["operation_id"],
                                 transport=make_transport(conn, fake))
    assert result["state"] == ("confirmed" if target == "new" else "rejected")
    assert len(fake.updates) == (1 if target == "new" else 0)


@pytest.mark.parametrize("lost_value", [False, True])
def test_final_recheck_detects_value_or_visibility_change_with_unchanged_marker(conn, setup, lost_value):
    cfg, op = prepare(conn, setup, progress=None)
    changed = missing_progress_pages() if lost_value else snapshot_pages(progress="colleague change")
    fake = WriteFake(snapshot_pages(progress=None) + changed)
    result = operations.dispatch(conn, cfg, operation_id=op["operation_id"],
                                 transport=make_transport(conn, fake))
    assert result["state"] == "rejected"
    assert fake.updates == []
    assert custody(conn)[0]["state"] == "precondition_failed"


def test_precondition_change_rejects_terminally_without_mutation(conn, setup):
    cfg, op = prepare(conn, setup)
    fake = WriteFake(snapshot_pages() + snapshot_pages(marker="moved"))
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=make_transport(conn, fake)
    )
    assert result["state"] == "rejected"
    payload = json.loads(result["result_json"])
    assert payload["evidence_ref"] == "write-attempt:precondition_failed"
    assert payload["applied_fields"] == []
    assert fake.updates == []
    rows = custody(conn)
    assert len(rows) == 1 and rows[0]["state"] == "precondition_failed"


def test_acknowledged_write_confirms_only_with_state_and_identity_correlation(
    conn, setup
):
    cfg, op = prepare(conn, setup)
    fake = WriteFake(
        snapshot_pages()
        + snapshot_pages()
        + snapshot_pages(progress="new")
        + history(op_record())
    )
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=make_transport(conn, fake)
    )
    assert result["state"] == "confirmed"
    assert len(fake.updates) == 1 and fake.updates[0][1] == {"progress": "new"}
    payload = json.loads(result["result_json"])
    assert payload["evidence_ref"].startswith("project-op-record:")
    assert payload["applied_fields"] == ["progress"]
    rows = custody(conn)
    assert len(rows) == 1 and rows[0]["state"] == "acknowledged"
    assert rows[0]["user_key"] == "writer-user"
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE project_write_attempts SET state='unknown'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM project_write_attempts")


def test_matching_values_without_identity_record_stay_unknown(conn, setup):
    cfg, op = prepare(conn, setup)
    fake = WriteFake(
        snapshot_pages()
        + snapshot_pages()
        + snapshot_pages(progress="new")
        + history(op_record(operator="someone-else"))
    )
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=make_transport(conn, fake)
    )
    assert result["state"] == "unknown"
    assert json.loads(result["result_json"]) == {"outcome": "unknown"}


def test_unknown_recheck_recovers_after_replication_delay(conn, setup):
    # Real contract: acknowledged writes become visible to snapshot/history
    # reads a few seconds later. The bounded recheck settles them without a
    # human; still-unknown outcomes keep requiring recorded settlement.
    cfg, op = prepare(conn, setup)
    fake = WriteFake(
        snapshot_pages()  # preflight
        + snapshot_pages()  # recheck-window token read
        + snapshot_pages()  # first reconcile: stale value -> unknown
        + snapshot_pages(progress="new")  # retry reconcile: fresh value
        + history(op_record())
    )
    naps = []
    result = operations.dispatch(
        conn,
        cfg,
        operation_id=op["operation_id"],
        transport=make_transport(conn, fake),
        unknown_recheck=(5.0, 10.0),
        sleep=naps.append,
    )
    assert result["state"] == "confirmed" and naps == [5.0]
    assert len(fake.updates) == 1


def test_remote_paragraph_newline_normalization_still_confirms(conn, setup):
    # Real contract: Meegle rich text reads "a\nb" back as "a\n\nb" (observed
    # 2026-09-18, issue 7117732933). Whitespace-run equivalence must confirm,
    # while actual content changes must stay unknown.
    cfg, op = prepare(conn, setup, change={"fields": {"progress": "line1\nline2"}})
    fake = WriteFake(
        snapshot_pages()
        + snapshot_pages()
        + snapshot_pages(progress="line1\n\nline2")
        + history(op_record())
    )
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=make_transport(conn, fake)
    )
    assert result["state"] == "confirmed"
    assert json.loads(result["result_json"])["outcome"] == "applied"


def test_content_difference_beyond_newlines_stays_unknown(conn, setup):
    cfg, op = prepare(conn, setup, change={"fields": {"progress": "line1\nline2"}})
    fake = WriteFake(
        snapshot_pages()
        + snapshot_pages()
        + snapshot_pages(progress="line1\n\nline2 edited")
        + history(op_record())
    )
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=make_transport(conn, fake)
    )
    assert result["state"] == "unknown"


def test_structured_select_and_user_readback_shapes_confirm():
    from k3_support.project_write_transport import _field_confirmed

    # Real K3 read-back shapes (2026-09-18, issue 7117732933): select written
    # as option id returns {label, value}; user written as key list returns
    # member objects. Mismatched ids/keys or empty writes stay unconfirmed.
    assert _field_confirmed({"label": "patch链接", "value": "eqt4pgy44"}, "eqt4pgy44")
    assert not _field_confirmed({"label": "x", "value": "other"}, "eqt4pgy44")
    members = [{"key": "7255", "name": "苟敏", "email": "a@b"}]
    assert _field_confirmed(members, ["7255"])
    assert not _field_confirmed(members, ["7255", "999"])
    assert not _field_confirmed(members, [])
    assert not _field_confirmed(None, ["7255"])


@pytest.mark.parametrize("response", [{"ok": True}, TimeoutError("response lost")])
def test_uncertain_call_is_not_confirmed_by_same_user_unrelated_history(conn, setup, response):
    cfg, op = prepare(conn, setup)
    fake = WriteFake(
        snapshot_pages() + snapshot_pages() + snapshot_pages(),
        response=response,
    )
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=make_transport(conn, fake)
    )
    assert result["state"] == "unknown" and len(fake.updates) == 1
    recovery = WriteFake(snapshot_pages(progress="new") + history(op_record()))
    settled = operations.reconcile(
        conn,
        operation_id=op["operation_id"],
        transport=make_transport(conn, recovery),
    )
    assert settled["state"] == "unknown"
    assert recovery.updates == []


def test_provider_exception_stays_unknown_and_settles_by_recorded_human_check(
    conn, setup
):
    cfg, op = prepare(conn, setup)
    fake = WriteFake(
        snapshot_pages() + snapshot_pages() + snapshot_pages(),
        response=TimeoutError("secret-provider-detail"),
    )
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=make_transport(conn, fake)
    )
    assert result["state"] == "unknown" and len(fake.updates) == 1
    rows = custody(conn)
    assert rows[0]["state"] == "unknown" and rows[0]["response_digest"] is None
    stored = json.dumps(dict(result)) + json.dumps(dict(rows[0]))
    assert "secret-provider-detail" not in stored
    settle(
        conn,
        operation_id=op["operation_id"],
        actor="owner",
        verdict="confirmed_not_applied",
        evidence_text="checked the item manually; no change was applied",
    )
    assert operations._operation(conn, op["operation_id"])["state"] == "rejected"


def test_transitions_are_never_advertised_without_a_metadata_contract(conn, setup):
    cfg, op = prepare(
        conn,
        setup,
        action="bug.close",
        change={"transition_id": "to-close", "target_status_id": "closed"},
    )
    fake = WriteFake(snapshot_pages() + [{}])
    with pytest.raises(BugConflict, match="transition changed"):
        operations.dispatch(
            conn,
            cfg,
            operation_id=op["operation_id"],
            transport=make_transport(
                conn,
                fake,
                transition_contract_verified=True,
                closing_status_ids=frozenset({"closed"}),
            ),
        )
    assert fake.transitions == [] and custody(conn) == []
    assert operations._operation(conn, op["operation_id"])["state"] == "prepared"


def test_preflight_advertises_strict_verified_transition_metadata(conn):
    fake = WriteFake(snapshot_pages() + [transition_page(transition_row()), {}])
    view = make_transport(
        conn,
        fake,
        closing_status_ids=frozenset({"TESTING"}),
        transition_contract_verified=True,
    ).preflight(
        {
            "host": fake.host,
            "project_key": "space",
            "type_key": "type",
            "item_id": "123",
        }
    )
    assert fake.calls[-2] == (
        "workflow.list-state-transitions",
        {
            "project_key": "space",
            "work_item_id": "123",
            "work_item_type": "type",
            "user_key": "writer-user",
        },
    )
    assert fake.calls[-1] == (
        "workflow.list-state-required",
        {
            "project_key": "space",
            "work_item_id": "123",
            "state_key": "TESTING",
            "mode": "unfinished",
        },
    )
    assert view.transitions == {
        "101": {
            "target_status_id": "TESTING",
            "closes": True,
            "required_complete": True,
        }
    }


def normalized_transitions(
    payload, *, closing_status_ids=frozenset(), unfinished=lambda state_key: False
):
    return _transitions(
        payload,
        {
            "host": "project.feishu.cn",
            "project_key": "space",
            "type_key": "type",
            "item_id": "123",
        },
        {
            "status_id": "CLOSED",
            "fields": {"progress": "ready", "owner": "alice", "empty": ""},
        },
        closing_status_ids,
        unfinished,
    )


def test_transition_normalizer_accepts_exact_happy_shape_and_closing_flag():
    assert normalized_transitions(
        transition_payload(
            transition_row(
                7,
                state_key="CLOSED",
                confirm_form=[{"class": "field", "key": "progress", "name": "Done"}],
            )
        ),
        closing_status_ids=frozenset({"CLOSED"}),
    ) == {
        "7": {
            "target_status_id": "CLOSED",
            "closes": True,
            "required_complete": True,
        }
    }


def test_transition_normalizer_rejects_state_mismatch():
    assert normalized_transitions(
        transition_payload(transition_row(), state_key="OPEN")
    ) == {}


def test_transition_normalizer_unknown_confirm_class_fails_required_data_closed():
    assert normalized_transitions(
        transition_payload(
            transition_row(confirm_form=[{"class": "bot", "key": "progress"}])
        )
    ) == {
        "101": {
            "target_status_id": "TESTING",
            "closes": False,
            "required_complete": False,
        }
    }


@pytest.mark.parametrize(
    "entry,required",
    [
        ({"class": "role", "key": "role_space_owner"}, True),
        ({"class": "role", "key": "role_other_owner"}, False),
    ],
)
def test_transition_normalizer_maps_role_keys_only_for_this_project(entry, required):
    assert normalized_transitions(
        transition_payload(transition_row(confirm_form=[entry]))
    ) == {
        "101": {
            "target_status_id": "TESTING",
            "closes": False,
            "required_complete": required,
        }
    }


def test_confirm_role_satisfied_by_observed_current_status_operator_pairing():
    # Real K3 contract (2026-09-18, OPEN -> IN PROGRESS): confirm_form names
    # role_<project>_issue_operator; the snapshot has no issue_operator field
    # but exposes current_status_operator_role (value = full role key) plus a
    # non-empty current_status_operator member list.
    payload = transition_payload(
        transition_row(
            confirm_form=[{"class": "role", "key": "role_space_issue_operator"}]
        )
    )
    destination = {
        "host": "project.feishu.cn",
        "project_key": "space",
        "type_key": "type",
        "item_id": "123",
    }
    fields = {
        "current_status_operator": [{"key": "u1", "name": "someone"}],
        "current_status_operator_role": [
            {"label": "经办人", "value": "role_space_issue_operator"}
        ],
    }
    snapshot = {"status_id": "CLOSED", "fields": fields}
    complete = lambda state_key: False
    assert _transitions(payload, destination, snapshot, frozenset(), complete) == {
        "101": {
            "target_status_id": "TESTING",
            "closes": False,
            "required_complete": True,
        }
    }
    without_members = {
        "status_id": "CLOSED",
        "fields": fields | {"current_status_operator": []},
    }
    assert _transitions(
        payload, destination, without_members, frozenset(), complete
    ) == {
        "101": {
            "target_status_id": "TESTING",
            "closes": False,
            "required_complete": False,
        }
    }


def test_transition_normalizer_rejects_duplicate_ids():
    assert normalized_transitions(
        transition_payload(transition_row(5), transition_row(5, state_key="DONE"))
    ) == {}


def test_required_complete_follows_official_answer_not_snapshot_emptiness():
    # Real K3 contract (2026-09-18): confirm_form shows optional form fields
    # (e.g. patch link/file) that list-state-required does not require, so an
    # empty snapshot field must not block; a pending official requirement or
    # an unreadable answer must block even with non-empty snapshot fields.
    payload = transition_payload(
        transition_row(confirm_form=[{"class": "field", "key": "empty"}])
    )
    assert normalized_transitions(payload)["101"]["required_complete"] is True
    filled = transition_payload(
        transition_row(confirm_form=[{"class": "field", "key": "progress"}])
    )
    for answer in (True, None):
        result = normalized_transitions(filled, unfinished=lambda sk, a=answer: a)
        assert result["101"]["required_complete"] is False


def test_official_required_answer_is_cached_per_target_state(conn):
    fake = WriteFake(
        snapshot_pages()
        + [
            transition_page(
                transition_row(7, state_key="TESTING"),
                transition_row(8, state_key="TESTING"),
                transition_row(9, state_key="DONE"),
            ),
            {},
            {"form_items": [{"class": "field", "key": "left", "finished": False}]},
        ]
    )
    view = make_transport(
        conn, fake, transition_contract_verified=True
    ).preflight(
        {
            "host": fake.host,
            "project_key": "space",
            "type_key": "type",
            "item_id": "123",
        }
    )
    required = [c for c in fake.calls if c[0] == "workflow.list-state-required"]
    assert [c[1]["state_key"] for c in required] == ["TESTING", "DONE"]
    assert view.transitions["7"]["required_complete"] is True
    assert view.transitions["8"]["required_complete"] is True
    assert view.transitions["9"]["required_complete"] is False


def test_unfinished_required_contract_shapes():
    from k3_support.project_write_transport import _unfinished_required

    destination = {
        "host": "project.feishu.cn",
        "project_key": "space",
        "type_key": "type",
        "item_id": "123",
    }

    def probe(payload):
        client = Client([payload])
        return _unfinished_required(client, lambda: None, destination, "RESOLVED")

    assert probe({}) is False
    assert (
        probe({"form_items": [{"class": "field", "key": "k", "finished": False}]})
        is True
    )
    assert probe({"form_items": []}) is False
    assert probe({"form_items": [{"key": ""}]}) is None
    assert probe({"form_items": "x"}) is None
    assert probe({"form_items": [], "extra": 1}) is None
    failing = Client([RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        _unfinished_required(failing, lambda: None, destination, "RESOLVED")


@pytest.mark.parametrize(
    "payload",
    [
        transition_payload(transition_row()) | {"extra": True},
        transition_payload(transition_row() | {"extra": True}),
        transition_payload(
            transition_row(confirm_form=[{"class": "field", "key": "progress", "x": 1}])
        ),
    ],
)
def test_transition_normalizer_rejects_extra_keys_anywhere(payload):
    assert normalized_transitions(payload) == {}


def test_write_client_uses_real_field_update_and_transition_argv(binary):
    from test_project_read_client import client as read_client

    native, calls = read_client(
        binary,
        [(0, {"mcp_result": ""}), (0, {"mcp_result": ""})],
    )
    native.__class__ = WriteClient
    destination = {
        "host": native.host,
        "project_key": "space",
        "type_key": "issue",
        "item_id": "123",
    }
    assert native.update_fields(destination, {"b": ["x"], "a": "plain"}) == {
        "mcp_result": ""
    }
    assert json.loads(calls[0][0][calls[0][0].index("--params") + 1]) == {
        "project_key": "space",
        "work_item_id": "123",
        "fields": [
            {"field_key": "a", "field_value": "plain"},
            {"field_key": "b", "field_value": '["x"]'},
        ],
    }
    assert native.transition_state(
        destination, {"transition_id": "42", "target_status_id": "TESTING"}
    ) == {"mcp_result": ""}
    assert json.loads(calls[1][0][calls[1][0].index("--params") + 1]) == {
        "project_key": "space",
        "work_item_id": "123",
        "transition_id": "42",
    }
    assert _ack("bug.fields", {"mcp_result": ""}) is True
    assert _ack("bug.transition", "success") is True
    assert _ack("bug.transition", {"mcp_result": ""}) is False


def test_write_client_rejects_invalid_transition_id_before_command(binary):
    from test_project_read_client import client as read_client

    native, calls = read_client(binary, [(0, {"mcp_result": ""})])
    native.__class__ = WriteClient
    destination = {
        "host": native.host,
        "project_key": "space",
        "type_key": "issue",
        "item_id": "123",
    }
    with pytest.raises(ProjectReadError, match="invalid_transition_id"):
        native.transition_state(
            destination, {"transition_id": "abc", "target_status_id": "TESTING"}
        )
    assert calls == []
