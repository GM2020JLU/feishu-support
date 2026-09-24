# ruff: noqa: F811 -- pytest injects the explicitly imported shared fixtures by name
import json

import pytest
from test_gui import console, login  # noqa: F401 -- shared authenticated HTTP fixture
from test_project_bug_operations import setup  # noqa: F401 -- isolated adapter fixture

from k3_support import project_bug_operations as ops
from k3_support import project_bugs as bugs


def test_grant_controls_require_auth_and_keep_server_actor(
    console, project_context, conn
):
    request, _ = console
    body, adapter = project_context
    row = conn.execute(
        "SELECT * FROM project_bug_grants WHERE grant_id=?", (body["grant_id"],)
    ).fetchone()
    payload = {
        "request_id": "http-grant",
        "scope": json.loads(row["scope_json"]),
        "expires_at": row["expires_at"],
    }
    assert request("/api/project-bugs/issue-grant", payload)[0] == 403
    cookie, csrf = login(request)
    assert request("/api/project-bugs/issue-grant", payload, cookie=cookie)[0] == 403
    assert (
        request(
            "/api/project-bugs/issue-grant",
            payload | {"actor": "other"},
            cookie=cookie,
            csrf=csrf,
        )[0]
        == 409
    )
    code, _, result = request(
        "/api/project-bugs/issue-grant", payload, cookie=cookie, csrf=csrf
    )
    assert code == 200 and result["status"] == "active"
    replay = request(
        "/api/project-bugs/issue-grant", payload, cookie=cookie, csrf=csrf
    )[2]
    assert replay == result
    changed = payload | {"scope": payload["scope"] | {"fields": ["different"]}}
    assert (
        request("/api/project-bugs/issue-grant", changed, cookie=cookie, csrf=csrf)[0]
        == 409
    )
    listing = request(
        "/api/project-bugs/list-grants",
        {"bug_id": body["bug_id"], "after_id": ""},
        cookie=cookie,
        csrf=csrf,
    )[2]
    assert result["grant_id"] in {item["grant_id"] for item in listing["items"]}
    assert (
        conn.execute(
            "SELECT actor FROM project_bug_grants WHERE grant_id=?",
            (result["grant_id"],),
        ).fetchone()[0]
        == "owner"
    )
    assert not adapter.writes and adapter.reads == 0


def test_web_revoke_is_idempotent_and_blocks_already_prepared_dispatch(
    console, project_context, conn, config
):
    request, _ = console
    body, adapter = project_context
    operation = ops.prepare(conn, **body)
    cookie, csrf = login(request)
    payload = {"grant_id": body["grant_id"]}
    assert request("/api/project-bugs/revoke-grant", payload, cookie=cookie)[0] == 403
    for _ in range(2):
        assert (
            request(
                "/api/project-bugs/revoke-grant", payload, cookie=cookie, csrf=csrf
            )[2]["status"]
            == "revoked"
        )
    assert (
        conn.execute(
            "SELECT count(*) FROM project_bug_grant_events WHERE grant_id=? AND kind='revoked'",
            (body["grant_id"],),
        ).fetchone()[0]
        == 1
    )
    row = conn.execute(
        "SELECT state FROM project_bug_operations WHERE operation_id=?",
        (operation["operation_id"],),
    ).fetchone()
    assert (
        row["state"] == "prepared"
    )  # Revocation does not fake cancellation or delivery.
    with pytest.raises(PermissionError, match="grant"):
        ops.dispatch(
            conn, config, operation_id=operation["operation_id"], transport=adapter
        )
    assert not adapter.writes and adapter.reads == 0


@pytest.fixture
def project_context(setup, config):
    cfg, request, adapter = setup
    config.raw.update(cfg.raw)
    return request, adapter


def test_bug_reads_require_login_and_csrf(console, project_context, config):
    request, _ = console
    body, _ = project_context
    assert request("/api/project-bugs/list", {"after_id": ""})[0] == 403
    cookie, csrf = login(request)
    assert (
        request("/api/project-bugs/detail", {"bug_id": body["bug_id"]}, cookie=cookie)[
            0
        ]
        == 403
    )
    code, _, result = request(
        "/api/project-bugs/detail", {"bug_id": body["bug_id"]}, cookie=cookie, csrf=csrf
    )
    assert code == 200
    assert result["snapshot_source"] == "local_cache"
    assert result["remote_dispatch_available"] is False
    assert result["plan_controls_available"] is True
    assert result["verification_catalog"] == {
        "repositories": sorted(config.raw["repositories"]),
        "node": config.runtime("remote_host"),
    }
    assert result["snapshot"]["closure"]["closed"] is False


def test_prepare_preview_cancel_has_no_transport_and_no_client_actor(
    console, project_context, conn
):
    request, _ = console
    body, adapter = project_context
    cookie, csrf = login(request)
    assert (
        request("/api/project-bugs/prepare-write", body, cookie=cookie, csrf=csrf)[0]
        == 409
    )
    body = {k: v for k, v in body.items() if k != "actor"}
    code, _, operation = request(
        "/api/project-bugs/prepare-write", body, cookie=cookie, csrf=csrf
    )
    assert code == 200 and operation["actor"] == "owner"
    code, _, result = request(
        "/api/project-bugs/detail", {"bug_id": body["bug_id"]}, cookie=cookie, csrf=csrf
    )
    assert code == 200
    assert result["operations"][0]["preview"]["differences"][0]["state"] == "change"
    cancel = {
        "operation_id": operation["operation_id"],
        "expected_digest": operation["request_digest"],
    }
    assert request("/api/project-bugs/cancel-write", cancel, cookie=cookie)[0] == 403
    assert (
        request("/api/project-bugs/cancel-write", cancel, cookie=cookie, csrf=csrf)[2][
            "state"
        ]
        == "cancelled"
    )
    assert (
        request("/api/project-bugs/dispatch", cancel, cookie=cookie, csrf=csrf)[0]
        == 409
    )
    assert not adapter.writes and adapter.reads == 0
    assert (
        conn.execute("SELECT count(*) FROM project_bug_operations").fetchone()[0] == 1
    )


def test_round_cannot_supersede_pending_write(console, project_context, conn):
    request, _ = console
    body, _ = project_context
    ops.prepare(conn, **body)
    cookie, csrf = login(request)
    code, _, result = request(
        "/api/project-bugs/start-round",
        {
            "bug_id": body["bug_id"],
            "reason": "New investigation",
            "request_id": "round-1",
            "expected_revision": 2,
        },
        cookie=cookie,
        csrf=csrf,
    )
    assert code == 409 and "unsettled" in result["error"]
    assert not bugs.detail(conn, body["bug_id"])["rounds"]


def test_bug_list_is_read_only_and_rejects_forged_settings(
    console, project_context, conn
):
    request, _ = console
    cookie, csrf = login(request)
    before = conn.total_changes
    code, _, result = request(
        "/api/project-bugs/list", {"after_id": ""}, cookie=cookie, csrf=csrf
    )
    assert code == 200 and len(result["items"]) == 1 and result["next_cursor"] is None
    assert conn.total_changes == before
    assert (
        request(
            "/api/project-bugs/list",
            {"after_id": "", "token": "client-value"},
            cookie=cookie,
            csrf=csrf,
        )[0]
        == 409
    )


def test_verification_plan_requires_authenticated_operator(console, conn):
    from test_project_bugs import binding
    from test_project_verification import definition

    request, _ = console
    bug = binding(conn)
    round_ = bugs.start_round(
        conn,
        bug_id=bug["bug_id"],
        actor="owner",
        request_id="round",
        reason="Synthetic",
        expected_revision=1,
    )
    payload = {
        "bug_id": bug["bug_id"],
        "round_id": round_["round_id"],
        "request_id": "plan",
        "expected_revision": 2,
        "plan": definition(),
    }
    route = "/api/project-bugs/publish-verification-plan"
    assert request(route, payload)[0] == 403
    cookie, csrf = login(request)
    assert request(route, payload, cookie=cookie)[0] == 403
    assert (
        request(route, payload | {"actor": "forged"}, cookie=cookie, csrf=csrf)[0]
        == 409
    )
    code, _, plan = request(route, payload, cookie=cookie, csrf=csrf)
    assert code == 200 and plan["bindings_verified"] is False
    detail = request(
        "/api/project-bugs/detail", {"bug_id": bug["bug_id"]}, cookie=cookie, csrf=csrf
    )[2]
    assert detail["verification_plans"] == [plan]
    assert detail["rounds"][0]["verification_state"] == "not_run"


def test_pending_write_blocks_plan_replacement(console, project_context, conn):
    from test_project_verification import definition

    request, _ = console
    body, _ = project_context
    round_ = bugs.start_round(
        conn,
        bug_id=body["bug_id"],
        actor="owner",
        request_id="round",
        reason="Synthetic",
        expected_revision=2,
    )
    body = body | {"expected_revision": 3}
    operation = ops.prepare(conn, **body)
    revision = bugs.detail(conn, body["bug_id"])["revision"]
    cookie, csrf = login(request)
    payload = {
        "bug_id": body["bug_id"],
        "round_id": round_["round_id"],
        "request_id": "plan",
        "expected_revision": revision,
        "plan": definition(),
    }
    response = request(
        "/api/project-bugs/publish-verification-plan", payload, cookie=cookie, csrf=csrf
    )
    assert response[0] == 409
    assert (
        conn.execute("SELECT count(*) FROM project_verification_plans").fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT state FROM project_bug_operations WHERE operation_id=?",
            (operation["operation_id"],),
        ).fetchone()[0]
        == "prepared"
    )


def test_verification_run_control_has_no_dispatch_or_result_input(
    console, conn, config
):
    from test_project_verification_runs import context

    execution = context.__wrapped__(conn, config)
    payload = {key: value for key, value in execution[-2].items() if key != "actor"}
    request, _ = console
    route = "/api/project-bugs/prepare-verification-run"
    assert request(route, payload)[0] == 403
    cookie, csrf = login(request)
    assert request(route, payload, cookie=cookie)[0] == 403
    assert (
        request(route, payload | {"verified": True}, cookie=cookie, csrf=csrf)[0] == 409
    )
    code, _, result = request(route, payload, cookie=cookie, csrf=csrf)
    assert code == 200 and result["execution_state"] == "prepared"
    assert result["receipt"] is None
    assert conn.execute("SELECT count(*) FROM broker_remote_actions").fetchone()[0] == 0
