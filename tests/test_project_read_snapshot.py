"""Synthetic pagination/identity/race cases; never accesses a real Project."""

import copy
import sqlite3
from datetime import timedelta

import pytest

from k3_support import project_bug_grants as grants
from k3_support import project_bug_sync as sync
from k3_support import project_bugs as bugs
from k3_support.project_read_client import ProjectReadError
from k3_support.project_read_snapshot import SnapshotReader
from k3_support.store import create_case
from k3_support.timeutil import iso_now, utc_now

DEST = {
    "host": "project.feishu.cn",
    "project_key": "space",
    "type_key": "issue",
    "item_id": "123",
}


def responses(page_size=100):
    metadata = {
        "list": [
            {"field_key": k, "field_name": k, "field_type": "text"}
            for k in ["name", "progress", "optional"]
        ],
        "pagination": {"has_more": False, "page_num": 1, "page_size": 50, "total": 3},
    }
    item = {
        "work_item_attribute": {
            "owned_project": {"key": "space"},
            "work_item_type": {"key": "issue"},
            "work_item_id": "123",
            "work_item_status": {"key": "CLOSED", "name": "VERIFIED"},
            "work_item_name": "Synthetic",
            "update_time": "opaque-marker",
            "work_item_mod": "状态流",
        },
        "work_item_fields": [{"key": "progress", "name": "Progress", "value": None}],
        "pagination": {"has_more": False, "page_size": page_size, "total": 3},
    }
    if page_size == 2:
        item["pagination"].update(has_more=True, next_page_token="progress")
        second = copy.deepcopy(item)
        second["pagination"].update(has_more=False)
        del second["pagination"]["next_page_token"]
        second["work_item_fields"] = []
        return [
            copy.deepcopy(value) for value in [metadata, item, second, metadata, item]
        ]
    return [copy.deepcopy(value) for value in [metadata, item, metadata, item]]


class Client:
    host = DEST["host"]

    def __init__(self, values=None, hook=None):
        self.values = values if values is not None else responses()
        self.calls = []
        self.hook = hook

    def read_page(self, command, params):
        self.calls.append((command, copy.deepcopy(params)))
        if self.hook:
            self.hook(len(self.calls))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return {"host": self.host, "command": command, "payload": value}


def test_logical_pagination_keeps_missing_distinct_from_null_and_terminal_unknown():
    client = Client(responses(2))
    result = SnapshotReader(client, page_size=2).collect(DEST)
    assert result["snapshot"]["fields"] == {
        "progress": None,
        "name": "Synthetic",
        "optional": None,
    }
    assert result["snapshot"]["closure"]["closed"] is None
    assert result["snapshot"]["remote_version"] is None
    evidence = result["read_evidence"]
    assert evidence["unobserved_field_keys"] == ["optional"]
    assert evidence["field_types"] == {
        "name": "text", "progress": "text", "optional": "text"
    }
    assert evidence["item_pages"] == 2 and evidence["logical_field_total"] == 3
    assert evidence["bookends_equal"] and evidence["pagination_complete"]
    assert not evidence["atomic_snapshot"]
    assert client.calls[2][1]["page_token"] == "progress"
    assert "page_token" not in client.calls[-1][1]


@pytest.mark.parametrize(
    "key,value",
    [
        ("work_item_id", "other"),
        ("owned_project", {"key": "other"}),
        ("work_item_type", {"key": "other"}),
    ],
)
def test_wrong_item_space_or_type_never_normalizes(key, value):
    data = responses()
    data[1]["work_item_attribute"][key] = value
    with pytest.raises(ProjectReadError, match="snapshot_identity_mismatch"):
        SnapshotReader(Client(data)).collect(DEST)


@pytest.mark.parametrize(
    "kind", ["status", "schema", "field", "total", "update_marker"]
)
def test_changed_bookends_or_pages_fail_without_retry(kind):
    data = responses(2)
    if kind == "status":
        data[2]["work_item_attribute"]["work_item_status"]["key"] = "OPEN"
    if kind == "schema":
        data[3]["list"][0]["field_type"] = "user"
    if kind == "field":
        data[4]["work_item_fields"][0]["value"] = "changed"
    if kind == "total":
        data[2]["pagination"]["total"] = 4
    if kind == "update_marker":
        data[2]["work_item_attribute"]["update_time"] = "new"
    client = Client(data)
    with pytest.raises(ProjectReadError, match="snapshot_changed_during_read"):
        SnapshotReader(client, page_size=2).collect(DEST)
    assert len(client.calls) <= 5


@pytest.mark.parametrize(
    "kind",
    [
        "field_duplicate",
        "schema_duplicate",
        "missing_cursor",
        "bool_total",
        "early_terminal",
    ],
)
def test_malformed_pagination_and_values_fail_closed(kind):
    data = responses(2)
    if kind == "field_duplicate":
        data[2]["work_item_fields"] = copy.deepcopy(data[1]["work_item_fields"])
    if kind == "schema_duplicate":
        data[0]["list"][1] = copy.deepcopy(data[0]["list"][0])
    if kind == "missing_cursor":
        del data[1]["pagination"]["next_page_token"]
    if kind == "bool_total":
        data[1]["pagination"]["total"] = True
    if kind == "early_terminal":
        data[1]["pagination"] = {"total": 3, "page_size": 2, "has_more": False}
    with pytest.raises(ProjectReadError):
        SnapshotReader(Client(data), page_size=2).collect(DEST)


def test_field_without_value_is_unobserved_and_not_explicit_null():
    data = responses()
    for index in [1, 3]:
        del data[index]["work_item_fields"][0]["value"]
    result = SnapshotReader(Client(data)).collect(DEST)
    # Defined-but-unset fields surface as None in the snapshot (they are the
    # backfill targets); the evidence keys keep missing distinct from null.
    assert result["snapshot"]["fields"] == {
        "name": "Synthetic",
        "optional": None,
        "progress": None,
    }
    assert result["read_evidence"]["omitted_value_field_keys"] == ["progress"]
    assert result["read_evidence"]["unobserved_field_keys"] == ["optional", "progress"]


@pytest.mark.parametrize("valid", [True, False])
def test_select_editor_options_reuse_strict_official_contract(valid):
    data = responses()
    for index in (0, 2):
        row = data[index]["list"][1]
        row["field_type"] = "select"
        row["option"] = [{"option_id": "2", "option_name": "P2"},
                         {"option_id": "1", "option_name": "P1"}]
        if not valid:
            row["option"][1]["option_id"] = "2"
    result = SnapshotReader(Client(data)).collect(DEST)
    choices = result["read_evidence"]["field_options"]
    assert choices == ({"progress": [{"value": "2", "label": "P2"},
                                     {"value": "1", "label": "P1"}]} if valid else {})
    assert result["snapshot"]["fields"]["progress"] is None


@pytest.mark.parametrize("members,known", [
    ([{"key":"person-1","name":"Test Operator","email":"excluded@example.com"}], True),
    ([], True), (None, False), ([{"name":"missing identity"}], False),
])
def test_role_display_keeps_observation_boundary_and_drops_directory_attributes(members, known):
    data = responses()
    for index in (1, 3):
        data[index]["work_item_attribute"]["role_members"] = [
            {"key":"operator","name":"经办人","members":members}
        ]
    result = SnapshotReader(Client(data)).collect(DEST)["read_evidence"]["role_membership"]
    assert result["complete_roster"] is False
    assert result["roles"][0]["members_observed"] is known
    assert "email" not in str(result)
    if not known:
        assert result["roles"][0]["members"] == []


@pytest.mark.parametrize("membership_changed", [False, True])
def test_role_record_order_is_irrelevant_but_membership_changes_are_not(
    membership_changed,
):
    data = responses()
    roles = [
        {"key": "reviewer", "members": ["one"]},
        {"key": "operator", "members": ["two"]},
    ]
    data[1]["work_item_attribute"]["role_members"] = copy.deepcopy(roles)
    data[3]["work_item_attribute"]["role_members"] = copy.deepcopy(
        list(reversed(roles))
    )
    if membership_changed:
        data[3]["work_item_attribute"]["role_members"][0]["members"] = ["different"]
        with pytest.raises(ProjectReadError, match="snapshot_changed_during_read"):
            SnapshotReader(Client(data)).collect(DEST)
    else:
        assert SnapshotReader(Client(data)).collect(DEST)["read_evidence"][
            "bookends_equal"
        ]


def test_wrong_host_and_budget_limits_are_not_silently_partial():
    client = Client()
    with pytest.raises(ProjectReadError, match="profile_host_mismatch"):
        SnapshotReader(client).collect(DEST | {"host": "other.example"})
    assert not client.calls
    data = responses()
    data[1]["work_item_fields"][0]["value"] = "x" * (8 * 1024 * 1024)
    with pytest.raises(ProjectReadError, match="snapshot_budget_exceeded"):
        SnapshotReader(Client(data)).collect(DEST)


def test_repeated_cursor_cannot_loop_or_return_partial_values():
    data = responses(2)
    for i in [1, 2]:
        data[i]["pagination"].update(total=6, has_more=True, next_page_token="progress")
    client = Client(data)
    with pytest.raises(ProjectReadError, match="invalid_snapshot_cursor"):
        SnapshotReader(client, page_size=2).collect(DEST)
    assert len(client.calls) == 3


def test_total_deadline_discards_even_a_successful_late_response(monkeypatch):
    from k3_support import project_read_snapshot

    ticks = iter([0, 1, 301])
    monkeypatch.setattr(project_read_snapshot.time, "monotonic", lambda: next(ticks))
    client = Client()
    with pytest.raises(ProjectReadError, match="snapshot_budget_exceeded"):
        SnapshotReader(client).collect(DEST)
    assert len(client.calls) == 1


def test_metadata_multiple_pages_are_complete_and_rechecked():
    data = responses()
    a = copy.deepcopy(data[0])
    b = copy.deepcopy(data[0])
    a["list"] = [
        {"field_key": "f" + str(i), "field_name": "F" + str(i), "field_type": "text"}
        for i in range(50)
    ]
    a["pagination"].update(has_more=True, total=53)
    b["pagination"].update(page_num=2, total=53)
    client = Client(copy.deepcopy([a, b, data[1], a, b, data[3]]))
    evidence = SnapshotReader(client).collect(DEST)["read_evidence"]
    assert evidence["metadata_field_count"] == 53 and evidence["metadata_pages"] == 2
    assert [p["page_num"] for c, p in client.calls if c == "workitem.meta-fields"] == [
        1,
        2,
        1,
        2,
    ]


@pytest.fixture
def bound(conn):
    case = create_case(
        conn, title="Synthetic", case_type="bug", severity="P2", confidence=1
    )[0]
    bug = bugs.bind(conn, case_id=case, actor="owner", **DEST)
    scope = {k: DEST[k] for k in ("host", "project_key", "type_key")}
    scope.update(
        bug_ids=[bug["bug_id"]],
        actions=["bug.read"],
        fields=[],
        transitions=[],
        repositories=[],
        devices=[],
    )
    grant = grants.issue(
        conn,
        actor="owner",
        request_id="read",
        scope=scope,
        expires_at=(utc_now() + timedelta(hours=1)).isoformat(),
    )
    return bug, {
        "bug_id": bug["bug_id"],
        "actor": "owner",
        "grant_id": grant["grant_id"],
        "observation_id": "refresh-one",
    }


def test_sync_persists_bound_evidence_replays_and_keeps_local_execution_unchanged(
    conn, bound
):
    bug, args = bound
    client = Client()
    first = sync.refresh(conn, client, **args)
    assert sync.refresh(conn, client, **args) == first
    assert len(client.calls) == 4
    detail = bugs.detail(conn, bug["bug_id"])
    assert detail["snapshot"]["read_evidence"]["atomic_snapshot"] is False
    assert detail["snapshot"]["fields"] == {
        "progress": None,
        "name": "Synthetic",
        "optional": None,
    }
    assert detail["rounds"] == []
    assert (
        conn.execute(
            "SELECT state FROM cases WHERE case_id=?", (bug["case_id"],)
        ).fetchone()[0]
        == "intake"
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE project_bug_read_evidence SET evidence_json='{}'")


@pytest.mark.parametrize("at", [0, 2, 4])
def test_revocation_before_during_or_after_reads_cannot_store_snapshot(conn, bound, at):
    _, args = bound

    def revoke():
        grants.revoke(conn, actor="owner", grant_id=args["grant_id"])

    if at == 0:
        revoke()
    client = Client(hook=lambda n: revoke() if n == at else None)
    with pytest.raises(PermissionError):
        sync.refresh(conn, client, **args)
    assert conn.execute("SELECT count(*) FROM project_bug_snapshots").fetchone()[0] == 0
    assert len(client.calls) == at


def test_concurrent_newer_snapshot_wins_without_partial_evidence(conn, bound):
    bug, args = bound

    def newer(n):
        if n == 4:
            bugs.observe(
                conn,
                bug_id=bug["bug_id"],
                observation_id="newer",
                expected_sequence=0,
                observed_at=iso_now(),
                payload={
                    "fields": {"name": "Newer"},
                    "status_id": "OPEN",
                    "closure": {"closed": None, "reason": None},
                    "remote_version": None,
                    "schema_digest": None,
                },
            )

    with pytest.raises(bugs.BugConflict, match="newer"):
        sync.refresh(conn, Client(hook=newer), **args)
    assert bugs.detail(conn, bug["bug_id"])["snapshot"]["fields"]["name"] == "Newer"
    assert (
        conn.execute("SELECT count(*) FROM project_bug_read_evidence").fetchone()[0]
        == 0
    )


def test_read_failure_leaves_existing_observation_and_does_not_clear_values(
    conn, bound
):
    bug, args = bound
    sync.refresh(conn, Client(), **args)
    broken = responses()
    broken[1] = ProjectReadError("remote_read_failed")
    with pytest.raises(ProjectReadError):
        sync.refresh(conn, Client(broken), **(args | {"observation_id": "failed"}))
    assert bugs.detail(conn, bug["bug_id"])["snapshot"]["sequence"] == 1
    assert (
        conn.execute("SELECT count(*) FROM project_bug_read_evidence").fetchone()[0]
        == 1
    )


def test_replay_does_not_bypass_revocation_or_accept_another_actor(conn, bound):
    _, args = bound
    client = Client()
    sync.refresh(conn, client, **args)
    with pytest.raises(PermissionError):
        sync.refresh(conn, client, **(args | {"actor": "other"}))
    grants.revoke(conn, actor="owner", grant_id=args["grant_id"])
    with pytest.raises(PermissionError):
        sync.refresh(conn, client, **args)
    assert len(client.calls) == 4


def test_diagnostic_item_command_passes_exact_identity_and_never_opens_database(
    monkeypatch, capsys
):
    import json

    from k3_support import project_read_cli

    client = Client()
    monkeypatch.setattr(project_read_cli, "MeegleReadClient", lambda **kwargs: client)
    code = project_read_cli.main(
        [
            "--executable",
            "/unused",
            "--sha256",
            "0" * 64,
            "--profile",
            "dedicated",
            "--host",
            DEST["host"],
            "read-item",
            "--project-key",
            "space",
            "--type-key",
            "issue",
            "--item-id",
            "123",
        ]
    )
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["destination"] == DEST
    assert result["snapshot"]["fields"] == {
        "progress": None,
        "name": "Synthetic",
        "optional": None,
    }
