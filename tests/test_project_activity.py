# ruff: noqa: F811 -- shared isolated fixtures
import copy
import json
import sqlite3
import threading
from datetime import timedelta

import pytest
from test_gui import console, login  # noqa: F401
from test_project_read_client import AUTH, binary, client  # noqa: F401
from test_project_read_snapshot import DEST, Client, bound  # noqa: F401
from test_project_refresh import READER

from k3_support import project_activity as activity
from k3_support import project_bug_grants as grants
from k3_support import project_bugs as bugs
from k3_support import project_refresh_service as service
from k3_support.project_bug_controls import execute
from k3_support.project_comments_reader import CommentsReader
from k3_support.project_history_reader import HistoryReader
from k3_support.project_read_client import ProjectReadError
from k3_support.runtime_control import ensure_global_state
from k3_support.timeutil import observed_clock, utc_now

END = 1800000000000


def record(n=1):
    return {
        "op_record_module": "field_mod",
        "operation_time": 1700000000000 + n,
        "operation_type": "modify",
        "operator": "opaque-user",
        "operator_type": "user",
        "project_key": "space",
        "work_item_id": 123,
        "work_item_type_key": "issue",
        "record_contents": [
            {
                "object": {"object_type": "field", "object_value": "priority"},
                "old": ["before"],
                "new": ["<script>after</script>"],
            }
        ],
        "source": None,
        "source_type": None,
    }


def history_page(ids=(1,), more=False, cursor=""):
    return {
        "op_records": [record(n) for n in ids],
        "has_more": more,
        "start_from": cursor,
        "total": 0,
    }


def comment(n):
    return {
        "comment_id": str(7000000000000000000 + n),
        "content": "<script>literal</script>",
        "created_at": "2026-06-16 15:26:31",
        "creator": "User label",
        "file_url": "",
    }


def comments_page(number=1, total=2):
    return {
        "comments": [
            comment(n) for n in range((number - 1) * 20, min(number * 20, total))
        ],
        "pagination": {
            "page_num": number,
            "page_size": 20,
            "total": total,
            "total_pages": (total + 19) // 20,
        },
    }


def test_history_uses_cursor_not_incorrect_total_and_preserves_opaque_changes():
    first, second = history_page([1, 2], True, "opaque"), history_page([3])
    client = Client([first, second, copy.deepcopy(first)])
    result = HistoryReader(client).collect(DEST, end=END)
    assert result["record_count"] == 3 and result["reported_totals"] == [0, 0]
    assert result["pagination_complete"] and not result["atomic_snapshot"]
    assert client.calls[1][1]["start_from"] == "opaque"
    assert (
        client.calls[0][1]
        == client.calls[-1][1]
        == {"project_key": "space", "work_item_id": "123", "start": 1, "end": END}
    )
    assert result["items"][0]["contents"][0]["new"] == ["<script>after</script>"]
    assert "opaque" not in json.dumps(result["reported_totals"])


@pytest.mark.parametrize(
    "kind",
    [
        "space",
        "type",
        "item",
        "boolean-id",
        "time",
        "future",
        "rows",
        "cursor",
        "more",
        "total",
    ],
)
def test_history_rejects_identity_and_pagination_ambiguity(kind):
    page = history_page()
    row = page["op_records"][0]
    if kind == "space":
        row["project_key"] = "other"
    if kind == "type":
        row["work_item_type_key"] = "other"
    if kind == "item":
        row["work_item_id"] = 456
    if kind == "boolean-id":
        row["work_item_id"] = True
    if kind == "time":
        row["operation_time"] = "1700000000001"
    if kind == "future":
        row["operation_time"] = END + 1
    if kind == "rows":
        page["op_records"] = {}
    if kind == "cursor":
        page.update(has_more=True, start_from="")
    if kind == "more":
        page["has_more"] = "false"
    if kind == "total":
        page["total"] = True
    with pytest.raises(ProjectReadError):
        HistoryReader(Client([page])).collect(DEST, end=END)


@pytest.mark.parametrize(
    "kind", ["cursor-loop", "repeated-page", "bookend", "page-budget"]
)
def test_history_cannot_claim_completeness_from_loops_changes_or_budget(kind):
    first = history_page([1], True, "a")
    if kind == "cursor-loop":
        pages = [first, history_page([2], True, "a")]
    elif kind == "repeated-page":
        pages = [first, history_page([1], True, "b")]
    elif kind == "bookend":
        pages = [history_page([1]), history_page([2])]
    else:
        pages = [history_page([n], True, str(n)) for n in range(20)]
    with pytest.raises(ProjectReadError):
        HistoryReader(Client(pages)).collect(DEST, end=END)


def test_comments_preserve_string_ids_literal_content_and_unknown_reply_hierarchy():
    first, second = comments_page(total=21), comments_page(2, 21)
    client = Client([first, second, copy.deepcopy(first)])
    result = CommentsReader(client).collect(DEST, end=END)
    assert result["record_count"] == 21 and result["pagination_complete"]
    assert result["items"][0]["comment_id"] == "7000000000000000000"
    assert result["items"][0]["created_at_display"] == "2026-06-16 15:26:31"
    assert not result["reply_hierarchy_verified"] and not result["atomic_snapshot"]
    assert client.calls[1][1]["page_num"] == 2
    assert all(c[1]["end_time"] == END for c in client.calls)


@pytest.mark.parametrize("total", [0, 1, 20])
def test_comment_single_and_empty_page(total):
    page = comments_page(total=total)
    result = CommentsReader(Client([page, copy.deepcopy(page)])).collect(DEST, end=END)
    assert result["record_count"] == total


@pytest.mark.parametrize(
    "kind",
    [
        "total",
        "pages",
        "number",
        "size",
        "duplicate",
        "numeric-id",
        "missing",
        "reply",
        "bad-content",
        "large-content",
    ],
)
def test_comments_reject_unsupported_or_partial_shapes(kind):
    page = comments_page()
    if kind == "total":
        page["pagination"]["total"] = 3
    if kind == "pages":
        page["pagination"]["total_pages"] = 2
    if kind == "number":
        page["pagination"]["page_num"] = 2
    if kind == "size":
        page["pagination"]["page_size"] = 50
    if kind == "duplicate":
        page["comments"][1] = copy.deepcopy(page["comments"][0])
    if kind == "numeric-id":
        page["comments"][0]["comment_id"] = 7000000000000000000
    if kind == "missing":
        del page["comments"][0]["content"]
    if kind == "reply":
        page["comments"][0]["replies"] = [comment(4)]
    if kind == "bad-content":
        page["comments"][0]["content"] = {}
    if kind == "large-content":
        page["comments"][0]["content"] = "x" * 65537
    with pytest.raises(ProjectReadError):
        CommentsReader(Client([page])).collect(DEST, end=END)


@pytest.mark.parametrize(
    "kind", ["duplicate-across-pages", "changed-count", "changed-bookend", "budget"]
)
def test_comment_read_does_not_merge_inconsistent_pages(kind):
    first = comments_page(total=21)
    second = comments_page(2, 21)
    if kind == "duplicate-across-pages":
        second["comments"][0] = copy.deepcopy(first["comments"][0])
    if kind == "changed-count":
        second = comments_page(2, 22)
    pages = [first, second, copy.deepcopy(first)]
    if kind == "changed-bookend":
        pages[-1]["comments"][0]["content"] = "edited"
    if kind == "budget":
        pages = [comments_page(n, 401) for n in range(1, 21)]
    with pytest.raises(ProjectReadError):
        CommentsReader(Client(pages)).collect(DEST, end=END)


@pytest.mark.parametrize(
    "command,params",
    [
        (
            "comment.list",
            {
                "project_key": "space",
                "work_item_id": "123",
                "page_num": 1,
                "end_time": END,
            },
        ),
        (
            "workitem.list-op-records",
            {
                "project_key": "space",
                "work_item_id": "123",
                "start": 1,
                "end": END,
                "start_from": "server-cursor",
            },
        ),
    ],
)
def test_native_read_boundary_accepts_only_reviewed_timestamp_cursor_parameters(
    binary, command, params
):
    c, calls = client(binary, [(0, AUTH), (0, {})])
    c.read_page(command, params)
    assert len(calls) == 2
    for key in ("session_id", "profile", "start"):
        with pytest.raises(ProjectReadError):
            c.read_page(command, params | {key: "bad"})
    bad = "end_time" if command == "comment.list" else "end"
    with pytest.raises(ProjectReadError):
        c.read_page(command, params | {bad: True})
    assert len(calls) == 2


@pytest.fixture
def context(conn, config, bound):
    config.raw["identity"]["control_operator_id"] = "owner"
    config.raw["project_integration"] = {
        "write_enabled": False,
        "reader": copy.deepcopy(READER),
    }
    ensure_global_state(
        conn, actor_id="owner", source="test", external_id="activity-init"
    )
    bug, args = bound
    return {
        "actor": "owner",
        "bug_id": bug["bug_id"],
        "grant_id": args["grant_id"],
        "request_id": "one",
        "kind": "history",
    }


def run(conn, config, kind="history", hook=None, values=None):
    page = history_page() if kind == "history" else comments_page()
    client = Client(
        values if values is not None else [page, copy.deepcopy(page)], hook=hook
    )
    return activity.run_one(conn, lambda: config, client_factory=lambda _: client)


@pytest.mark.parametrize("kind", ["history", "comments"])
def test_activity_roundtrip_is_private_immutable_and_never_advances_bug(
    conn, config, context, kind
):
    context = context | {"kind": kind}
    before = bugs.detail(conn, context["bug_id"])
    queued = activity.enqueue(conn, config, **context)
    assert activity.enqueue(conn, config, **context) == queued
    with pytest.raises(ValueError):
        activity.status(conn, actor="other", activity_id=queued["activity_id"])
    result = run(conn, config, kind)
    assert result["state"] == "succeeded" and result["kind"] == kind
    assert activity.enqueue(conn, config, **context) == result
    assert "items" not in result["observation"]
    page = activity.page(
        conn, actor="owner", activity_id=result["activity_id"], offset=0
    )
    assert (
        len(page["items"]) == (1 if kind == "history" else 2)
        and page["next_offset"] is None
    )
    assert page["kind"] == kind
    after = bugs.detail(conn, context["bug_id"])
    assert (before["revision"], before["snapshot"], before["rounds"]) == (
        after["revision"],
        after["snapshot"],
        after["rounds"],
    )
    for table in ("project_bug_operations", "jobs"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE project_activity_requests SET result_json='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE project_activity_requests SET kind='comments'"
            if kind == "history"
            else "UPDATE project_activity_requests SET kind='history'"
        )
    assert activity.run_one(conn, lambda: config) == {"state": "idle"}


def test_two_kinds_share_grant_but_not_request_identity_or_pending_lock(
    conn, config, context
):
    activity.enqueue(conn, config, **context)
    with pytest.raises(bugs.BugConflict):
        activity.enqueue(conn, config, **(context | {"kind": "comments"}))
    with pytest.raises(bugs.BugConflict):
        activity.enqueue(conn, config, **(context | {"request_id": "two"}))
    second = activity.enqueue(
        conn, config, **(context | {"request_id": "two", "kind": "comments"})
    )
    assert second["kind"] == "comments"


@pytest.mark.parametrize(
    "kind", ["revoked", "expired", "mode", "operator", "profile", "generation", "lease"]
)
def test_late_result_cannot_survive_authority_change(conn, config, context, kind):
    activity.enqueue(conn, config, **context)

    def hook(n):
        if n != 2:
            return
        if kind == "revoked":
            grants.revoke(conn, grant_id=context["grant_id"], actor="owner")
        if kind == "expired":
            conn.execute(
                "UPDATE project_activity_requests SET lease_expires_at=?",
                ((utc_now() - timedelta(seconds=1)).isoformat(),),
            )
        if kind == "mode":
            config.raw["mode"] = "drain"
        if kind == "operator":
            config.raw["identity"]["control_operator_id"] = "other"
        if kind == "profile":
            config.raw["project_integration"]["reader"]["profile"] = "different"
        if kind == "generation":
            conn.execute("UPDATE global_control_state SET revision=revision+2")
        if kind == "lease":
            conn.execute("UPDATE project_activity_requests SET lease_token='new-owner'")

    result = run(conn, config, hook=hook)
    assert result["state"] == ("running" if kind == "lease" else "blocked")
    assert result["observation"] is None


def test_expired_grant_stops_queued_read_and_unknown_lease_can_reclaim(
    conn, config, context
):
    queued = activity.enqueue(conn, config, **context)
    with observed_clock(utc_now() + timedelta(hours=2)):
        assert run(conn, config)["state"] == "blocked"
    assert (
        activity.enqueue(conn, config, **context)["activity_id"]
        == queued["activity_id"]
    )
    second = activity.enqueue(conn, config, **(context | {"request_id": "two"}))
    conn.execute(
        "UPDATE project_activity_requests SET state='running',attempt=1,lease_token='gone',lease_expires_at=? WHERE activity_id=?",
        ((utc_now() - timedelta(seconds=1)).isoformat(), second["activity_id"]),
    )
    result = run(conn, config)
    assert result["state"] == "succeeded" and result["attempt"] == 2


def test_stop_retry_limit_paging_and_kind_inputs_are_bounded(conn, config, context):
    stopped = threading.Event()
    stopped.set()
    assert activity.run_one(conn, lambda: config, stop_event=stopped) == {
        "state": "stopped"
    }
    for kind in (None, [], {}, "write"):
        with pytest.raises(ValueError):
            activity.enqueue(conn, config, **(context | {"kind": kind}))
    activity.enqueue(conn, config, **context)
    conn.execute("UPDATE project_activity_requests SET attempt=3")
    result = run(conn, config)
    assert (
        result["state"] == "failed" and result["error_code"] == "interrupted_read_limit"
    )
    for offset in (-1, True, 1, 10001):
        with pytest.raises(ValueError):
            activity.page(
                conn, actor="owner", activity_id=result["activity_id"], offset=offset
            )


def test_local_pagination_is_bound_to_one_accepted_observation(conn, config, context):
    first, second = comments_page(total=21), comments_page(2, 21)
    activity.enqueue(conn, config, **(context | {"kind": "comments"}))
    result = run(conn, config, "comments", values=[first, second, copy.deepcopy(first)])
    a = activity.page(conn, actor="owner", activity_id=result["activity_id"], offset=0)
    b = activity.page(
        conn, actor="owner", activity_id=result["activity_id"], offset=a["next_offset"]
    )
    assert len(a["items"]) == 20 and len(b["items"]) == 1 and b["next_offset"] is None
    with pytest.raises(ValueError):
        activity.page(conn, actor="other", activity_id=result["activity_id"], offset=0)


def test_authenticated_controls_and_background_routing(
    conn, config, context, console, monkeypatch
):
    http, _ = console
    body = {k: v for k, v in context.items() if k != "actor"}
    assert http("/api/project-bugs/activity-read", body)[0] == 403
    cookie, csrf = login(http)
    assert http("/api/project-bugs/activity-read", body, cookie=cookie)[0] == 403
    for extra in ("actor", "end", "start_from", "page_num", "profile"):
        assert (
            http(
                "/api/project-bugs/activity-read",
                body | {extra: "bad"},
                cookie=cookie,
                csrf=csrf,
            )[0]
            == 409
        )
    code, _, queued = http(
        "/api/project-bugs/activity-read", body, cookie=cookie, csrf=csrf
    )
    assert code == 200 and queued["kind"] == "history"
    monkeypatch.setattr(activity, "run_one", lambda *a, **k: {"route": "activity"})
    assert service.run_one(conn, lambda: config) == {"route": "activity"}
    detail = execute(
        conn, config, action="detail", payload={"bug_id": context["bug_id"]}
    )
    assert detail["activity_control"]["requests"] == [queued]
    assert detail["activity_control"]["grants"][0]["grant_id"] == context["grant_id"]


@pytest.mark.parametrize(
    "params",
    [{"start": 1}, {"end": END}, {"start": END, "end": END}, {"start": 0, "end": END}],
)
def test_history_native_range_requires_both_ordered_nonzero_bounds(binary, params):
    c, calls = client(binary)
    with pytest.raises(ProjectReadError):
        c.read_page(
            "workitem.list-op-records",
            {"project_key": "space", "work_item_id": "123", **params},
        )
    assert calls == []
