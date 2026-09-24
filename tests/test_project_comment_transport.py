# ruff: noqa: F811 -- shared isolated fixtures
import copy
import json
import sqlite3

import pytest
from test_project_activity import comments_page
from test_project_bug_operations import setup  # noqa: F401
from test_project_read_client import binary  # noqa: F401
from test_project_read_client import client as read_client
from test_project_read_snapshot import Client, responses

from k3_support import project_bug_operations as operations
from k3_support import project_bugs as bugs
from k3_support.project_comment_transport import CommentClient, CommentTransport, _ack
from k3_support.project_read_client import ProjectReadError
from k3_support.project_read_snapshot import SnapshotReader
from k3_support.timeutil import iso_now

FINGERPRINT = "a" * 64


def snapshot_pages():
    values = responses()
    for index in (1, 3):
        values[index]["work_item_attribute"]["work_item_type"]["key"] = "type"
    return values


def comments(
    content=None, comment_id="7000000000000000001", *, creator="writer-user"
):
    value = comments_page(total=0) if content is None else comments_page(total=1)
    if content is not None:
        value["comments"][0]["content"] = content
        value["comments"][0]["comment_id"] = comment_id
        value["comments"][0]["creator"] = creator
    return [value, copy.deepcopy(value)]


class ClientWithWrites(Client):
    user_key = "writer-user"

    def __init__(self, values, response=None):
        super().__init__(values)
        self.writes = []
        self.response = (
            response if response is not None else {"action": "create", "success": True}
        )

    def create_comment(self, destination, text):
        self.writes.append((copy.deepcopy(destination), text))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def prepare(
    conn,
    setup,
    *,
    observed="Reviewed conclusion",
    response=None,
    old=None,
    creator="writer-user",
):
    cfg, request, adapter = setup
    destination = adapter.view.destination
    snapshot = SnapshotReader(Client(snapshot_pages())).collect(destination)["snapshot"]
    observation = bugs.observe(
        conn,
        bug_id=request["bug_id"],
        observation_id="fresh",
        expected_sequence=1,
        payload=snapshot,
        observed_at=iso_now(),
    )
    request = request | {
        "action": "bug.comment",
        "change": {"text": "Reviewed conclusion"},
        "snapshot_id": observation["snapshot_id"],
        "expected_revision": bugs.detail(conn, request["bug_id"])["revision"],
    }
    op = operations.prepare(conn, **request)
    fake = ClientWithWrites(
        snapshot_pages()
        + comments(old, creator=creator)
        + snapshot_pages()
        + comments(observed, creator=creator),
        response=response,
    )
    transport = CommentTransport(
        conn,
        fake,
        reader_digest=FINGERPRINT,
        before_read=lambda: None,
        create_contract_verified=True,
    )
    return cfg, op, transport, fake


def test_official_adapter_confirms_only_new_ack_and_exact_new_readback(
    conn, setup
):
    cfg, op, transport, fake = prepare(conn, setup)
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=transport
    )
    assert result["state"] == "confirmed"
    assert len(fake.writes) == 1 and fake.writes[0][1] == "Reviewed conclusion"
    ack = conn.execute("SELECT * FROM project_comment_attempts").fetchone()
    assert ack["state"] == "acknowledged" and ack["comment_id"] is None
    assert (
        json.loads(result["result_json"])["evidence_ref"]
        == "project-comment:7000000000000000001"
    )
    assert (
        operations.reconcile(
            conn, operation_id=op["operation_id"], transport=transport
        )["state"]
        == "confirmed"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE project_comment_attempts SET comment_id='other'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM project_comment_attempts")


@pytest.mark.parametrize(
    "response",
    [
        TimeoutError("private secret"),
        {"ok": True},
        {"comment_id": 7000000000000000001},
        {"data": {"comment_id": "7000000000000000001"}},
    ],
)
def test_unknown_or_unaccepted_ack_stays_unknown_even_if_text_exists(
    conn, setup, response
):
    cfg, op, transport, fake = prepare(conn, setup, response=response)
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=transport
    )
    assert result["state"] == "unknown" and len(fake.writes) == 1
    packet = json.loads(result["write_json"])
    with pytest.raises(ValueError):
        transport.write(packet)
    assert transport.reconcile(packet).outcome == "unknown" and len(fake.writes) == 1
    assert "private secret" not in str(
        [tuple(r) for r in conn.execute("SELECT * FROM project_comment_attempts")]
    )


@pytest.mark.parametrize("observed", [None, "Edited by colleague"])
def test_acknowledged_create_without_exact_readback_is_not_confirmed(
    conn, setup, observed
):
    cfg, op, transport, fake = prepare(conn, setup, observed=observed)
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=transport
    )
    assert result["state"] == "unknown" and len(fake.writes) == 1


def test_ack_for_preexisting_comment_cannot_confirm_this_write(conn, setup):
    cfg, op, transport, _fake = prepare(conn, setup, old="Reviewed conclusion")
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=transport
    )
    assert result["state"] == "unknown"
    assert (
        conn.execute("SELECT comment_id FROM project_comment_attempts").fetchone()[0]
        is None
    )


def test_restarted_adapter_reads_ack_without_reissuing_write(conn, setup):
    cfg, op, transport, _fake = prepare(conn, setup, observed=None)
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=transport
    )
    assert result["state"] == "unknown"
    resumed = ClientWithWrites(
        snapshot_pages() + comments("Reviewed conclusion", creator="writer-user")
    )
    restarted = CommentTransport(
        conn,
        resumed,
        reader_digest=FINGERPRINT,
        before_read=lambda: None,
        create_contract_verified=True,
    )
    assert (
        operations.reconcile(
            conn, operation_id=op["operation_id"], transport=restarted
        )["state"]
        == "confirmed"
    )
    assert resumed.writes == []


def test_profile_change_never_confirms_previous_identity_effect(conn, setup):
    cfg, op, transport, _fake = prepare(conn, setup, observed=None)
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=transport
    )
    resumed = ClientWithWrites([])
    restarted = CommentTransport(
        conn, resumed, reader_digest="b" * 64, before_read=lambda: None
    )
    assert restarted.reconcile(json.loads(result["write_json"])).outcome == "unknown"
    assert resumed.calls == resumed.writes == []


def test_direct_forged_or_undispatched_packet_cannot_write(conn, setup):
    _cfg, op, transport, fake = prepare(conn, setup)
    with pytest.raises(ValueError):
        transport.write({"operation_id": op["operation_id"], "action": "bug.comment"})
    assert (
        fake.writes == []
        and conn.execute("SELECT count(*) FROM project_comment_attempts").fetchone()[0]
        == 0
    )


def test_comment_client_exact_create_only_no_automatic_replay(binary):
    native, calls = read_client(binary, [(0, {"action": "create", "success": True})])
    native.__class__ = CommentClient
    destination = {
        "host": native.host,
        "project_key": "space",
        "type_key": "issue",
        "item_id": "123",
    }
    assert native.create_comment(destination, "Reviewed") == {
        "action": "create",
        "success": True,
    }
    params = json.loads(calls[0][0][calls[0][0].index("--params") + 1])
    assert params == {
        "project_key": "space",
        "work_item_id": "123",
        "action": "create",
        "content": "Reviewed",
    }
    assert len(calls) == 1
    with pytest.raises(ProjectReadError):
        native.create_comment(
            destination | {"item_id": "name instead of ID"}, "Reviewed"
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {"id": "1"},
        {"comment_id": ""},
        {"comment_id": True},
        {"comment_id": "1", "other": "unknown"},
    ],
)
def test_ack_parser_never_guesses_output_schema(response):
    assert _ack(response) is False


def test_ack_parser_accepts_only_real_create_success_shape():
    assert _ack({"action": "create", "success": True}) is True


def test_unverified_native_contract_leaves_prepared_operation_unsent(conn, setup):
    cfg, op, transport, fake = prepare(conn, setup)
    transport.create_contract_verified = False
    with pytest.raises(PermissionError, match="permission"):
        operations.dispatch(
            conn, cfg, operation_id=op["operation_id"], transport=transport
        )
    assert operations._operation(conn, op["operation_id"])["state"] == "prepared"
    assert fake.writes == []
    assert (
        conn.execute("SELECT count(*) FROM project_comment_attempts").fetchone()[0] == 0
    )


def test_native_contract_gate_is_closed_by_default(conn):
    transport = CommentTransport(
        conn, Client([]), reader_digest=FINGERPRINT, before_read=lambda: None
    )
    with pytest.raises(PermissionError, match="not been accepted"):
        transport.write({})


def test_crash_reservation_cannot_be_replayed_or_inferred_from_text(conn, setup):
    cfg, op, transport, fake = prepare(conn, setup)

    def crash(*args):
        raise KeyboardInterrupt()

    fake.create_comment = crash
    with pytest.raises(KeyboardInterrupt):
        operations.dispatch(
            conn, cfg, operation_id=op["operation_id"], transport=transport
        )
    row = operations._operation(conn, op["operation_id"])
    assert row["state"] == "dispatched"
    assert (
        conn.execute("SELECT state FROM project_comment_attempts").fetchone()[0]
        == "reserved"
    )
    restarted = CommentTransport(
        conn, Client([]), reader_digest=FINGERPRINT, before_read=lambda: None
    )
    assert (
        operations.reconcile(
            conn, operation_id=op["operation_id"], transport=restarted
        )["state"]
        == "unknown"
    )


@pytest.mark.parametrize("action", ["bug.fields", "bug.transition", "bug.close"])
def test_server_permission_deferral_without_conditional_contract_never_mutates(
    conn, setup, action
):
    from dataclasses import replace

    cfg, request, adapter = setup
    change = (
        {"fields": {"progress": "new"}}
        if action == "bug.fields"
        else {"transition_id": "to-test", "target_status_id": "testing"}
    )
    op = operations.prepare(conn, **(request | {"action": action, "change": change}))
    adapter.view = replace(
        adapter.view,
        allowed_actions=frozenset(),
        server_enforced_actions=frozenset({action}),
        conditional_actions=frozenset(),
        conditional_token=None,
    )
    with pytest.raises(PermissionError):
        operations.dispatch(
            conn, cfg, operation_id=op["operation_id"], transport=adapter
        )
    assert adapter.writes == []


def test_overclaiming_server_enforced_transport_is_distrusted_entirely(conn, setup):
    from dataclasses import replace

    cfg, request, adapter = setup
    op = operations.prepare(conn, **request)
    adapter.view = replace(
        adapter.view,
        allowed_actions=frozenset(),
        server_enforced_actions=frozenset({"bug.fields", "bug.acl"}),
    )
    with pytest.raises(PermissionError):
        operations.dispatch(
            conn, cfg, operation_id=op["operation_id"], transport=adapter
        )
    assert adapter.writes == []
