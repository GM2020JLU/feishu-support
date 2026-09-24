# ruff: noqa: F811 -- shared isolated fixtures
import copy
import json
from datetime import timedelta

import pytest
from test_gui import console, login  # noqa: F401
from test_project_link_intake import SPACE
from test_project_read_client import AUTH, binary, client  # noqa: F401
from test_project_refresh import READER

from k3_support import project_bug_search as search
from k3_support import project_refresh_service as service
from k3_support.config import ConfigError, validate_config
from k3_support.project_bug_controls import execute
from k3_support.project_bug_query import compile_query, normalize, validate_scope
from k3_support.project_bugs import BugConflict
from k3_support.project_read_client import ProjectReadError
from k3_support.runtime_control import ensure_global_state
from k3_support.timeutil import iso_now, utc_now

SCOPE = {
    "simple_name": "k3",
    "project_key": "space",
    "type_key": "issue",
    "allowed_item_ids": [123, 456],
}


def raw(ids=(123, 456)):
    rows = []
    for key in ids:
        fields = [
            {
                "key": "work_item_id",
                "value_type": "long_value",
                "value": {"long_value": key},
            },
            {
                "key": "name",
                "value_type": "string_value",
                "value": {"string_value": "<script>literal</script>"},
            },
            {
                "key": "work_item_status",
                "value_type": "key_label_value_list",
                "value": {
                    "key_label_value_list": [{"key": "custom", "label": "Custom state"}]
                },
            },
        ]
        if len(rows) % 2:
            fields.reverse()
        rows.append({"moql_field_list": fields})
    return {
        "data": {"1": rows} if rows else {},
        "list": [{"count": len(rows), "group_infos": [{"group_id": "1"}]}]
        if rows
        else None,
        "search_status_info": None,
        "session_id": "NEVER_RETURN_THIS",
    }


def test_compiler_quotes_literals_keeps_mandatory_scope_and_has_no_raw_escape_hatch():
    params = compile_query(SCOPE, keyword="test_%'; SELECT `secret` --", after_id=123)
    assert set(params) == {"project_key", "mql"}
    assert "`work_item_id` > 123 AND `work_item_id` IN (123, 456)" in params["mql"]
    assert "LIKE '%test\\_\\%''; SELECT `secret` --%'" in params["mql"]
    assert params["mql"].endswith("ORDER BY `work_item_id` ASC LIMIT 50")


@pytest.mark.parametrize(
    "changes",
    [
        {"project_key": "x`.`other"},
        {"type_key": "a; DROP"},
        {"simple_name": "../x"},
        {"allowed_item_ids": []},
        {"allowed_item_ids": [True]},
        {"allowed_item_ids": [1, 1]},
        {"allowed_item_ids": [0]},
        {"allowed_item_ids": [2**63]},
        {"allowed_item_ids": ["123"]},
        {"session_id": "x"},
    ],
)
def test_scope_rejects_injection_ambiguity_and_overflow(changes):
    with pytest.raises(ValueError):
        validate_scope(SCOPE | changes)


@pytest.mark.parametrize(
    "keyword,after",
    [
        ("x\\y", 0),
        ("x\ny", 0),
        (" x", 0),
        ("x" * 201, 0),
        ("", True),
        ("", -1),
        ("", 2**63),
        ("", "123"),
    ],
)
def test_query_filter_is_bounded(keyword, after):
    with pytest.raises(ValueError):
        compile_query(SCOPE, keyword=keyword, after_id=after)


def test_normalization_is_keyed_and_does_not_assign_status_semantics_or_session():
    result = normalize(raw(), SCOPE)
    assert [x["item_id"] for x in result["items"]] == ["123", "456"]
    assert result["items"][0]["status"] == {"key": "custom", "label": "Custom state"}
    assert "NEVER_RETURN_THIS" not in json.dumps(result)
    assert result["next_after_id"] is None and result["snapshot_consistent"] is False
    assert normalize(raw([]), SCOPE)["items"] == []
    full = normalize(raw(range(1, 51)), SCOPE | {"allowed_item_ids": None})
    assert full["next_after_id"] == 50


@pytest.mark.parametrize(
    "kind",
    [
        "outside",
        "duplicate",
        "unordered",
        "cursor",
        "missing",
        "duplicate-field",
        "bad-type",
        "group",
        "count",
        "pending",
        "too-many",
        "null-name",
        "bad-status",
    ],
)
def test_response_cannot_launder_partial_or_out_of_scope_results(kind):
    payload, scope, after = raw(), SCOPE, 0
    if kind == "outside":
        payload = raw([789])
    if kind == "duplicate":
        payload = raw([123, 123])
    if kind == "unordered":
        payload = raw([456, 123])
    if kind == "cursor":
        after = 123
    if kind == "missing":
        payload["data"]["1"][0]["moql_field_list"].pop()
    if kind == "duplicate-field":
        payload["data"]["1"][0]["moql_field_list"].append(
            payload["data"]["1"][0]["moql_field_list"][0]
        )
    if kind == "bad-type":
        payload["data"]["1"][0]["moql_field_list"][0]["value"]["long_value"] = "123"
    if kind == "group":
        payload["data"]["2"] = []
    if kind == "count":
        payload["list"][0]["count"] = 100
    if kind == "pending":
        payload["search_status_info"] = {"state": "pending"}
    if kind == "too-many":
        payload, scope = raw(range(1, 52)), SCOPE | {"allowed_item_ids": None}
    if kind == "null-name":
        payload["data"]["1"][0]["moql_field_list"][1]["value"]["string_value"] = None
    if kind == "bad-status":
        payload["data"]["1"][0]["moql_field_list"][2]["value"][
            "key_label_value_list"
        ] = []
    with pytest.raises(ProjectReadError, match="invalid_query_response"):
        normalize(payload, scope, after_id=after)


def test_native_client_boundary_checks_auth_and_never_exposes_raw_query_api(binary):
    c, calls = client(binary, [(0, AUTH), (0, raw())])
    result = c.query_bugs(SCOPE)
    assert result["host"] == "project.feishu.cn"
    assert len(calls) == 2 and calls[1][0][3:5] == ["workitem", "query"]
    with pytest.raises(ProjectReadError, match="invalid_read_request"):
        c.read_page("workitem.query", {"project_key": "space", "mql": "raw"})
    assert len(calls) == 2


@pytest.fixture
def context(conn, config):
    config.raw["identity"]["control_operator_id"] = "owner"
    config.raw["project_integration"] = {
        "write_enabled": False,
        "reader": copy.deepcopy(READER),
        "search_spaces": [copy.deepcopy(SCOPE)],
    }
    ensure_global_state(
        conn, actor_id="owner", source="test", external_id="search-init"
    )
    return {
        "actor": "owner",
        "simple_name": "k3",
        "type_key": "issue",
        "keyword": "",
        "request_id": "one",
    }


class QueryClient:
    host = "project.feishu.cn"

    def __init__(self, hook=None, ids=(123, 456)):
        self.hook, self.ids, self.calls = hook, ids, []

    def read_page(self, command, params):
        self.calls.append((command, params))
        return {"host": self.host, "command": command, "payload": copy.deepcopy(SPACE)}

    def query_bugs(self, scope, **params):
        self.calls.append(("query", copy.deepcopy(scope), params))
        if self.hook:
            self.hook()
        return normalize(raw(self.ids), scope, after_id=params["after_id"]) | {
            "host": self.host,
            "observed_at": iso_now(),
        }


def run(conn, config, fake=None):
    return search.run_one(
        conn, lambda: config, client_factory=lambda _: fake or QueryClient()
    )


def test_search_is_actor_private_durable_and_does_not_create_cases_or_authorize_writes(
    conn, config, context
):
    queued = search.enqueue(conn, config, **context)
    assert search.enqueue(conn, config, **context) == queued
    with pytest.raises(BugConflict):
        search.enqueue(conn, config, **(context | {"keyword": "other"}))
    with pytest.raises(ValueError):
        search.status(conn, actor="other", search_id=queued["search_id"])
    fake = QueryClient()
    done = run(conn, config, fake)
    assert done["state"] == "succeeded" and done["attempt"] == 1
    assert (
        done["result"]["items"][0]["url"]
        == "https://project.feishu.cn/k3/issue/detail/123"
    )
    assert fake.calls[0][0] == "project.search"
    assert search.enqueue(conn, config, **context) == done
    assert run(conn, config) == {"state": "idle"}
    for table in (
        "cases",
        "project_bugs",
        "project_bug_grants",
        "jobs",
        "project_bug_operations",
    ):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert search.options(conn, config, actor="owner")["history"] == [done]
    assert search.options(conn, config, actor="other")["history"] == []


@pytest.mark.parametrize("change", ["scope", "operator", "disabled", "expiry", "mode"])
def test_mid_read_authority_changes_discard_results(conn, config, context, change):
    search.enqueue(conn, config, **context)

    def mutate():
        if change == "scope":
            config.raw["project_integration"]["search_spaces"][0][
                "allowed_item_ids"
            ] = [123]
        if change == "operator":
            config.raw["identity"]["control_operator_id"] = "other"
        if change == "disabled":
            config.raw["project_integration"]["reader"]["enabled"] = False
        if change == "mode":
            config.raw["mode"] = "drain"
        if change == "expiry":
            conn.execute(
                "UPDATE project_search_requests SET lease_expires_at=?",
                ((utc_now() - timedelta(seconds=1)).isoformat(),),
            )

    done = run(conn, config, QueryClient(mutate))
    assert done["state"] == "blocked" and done["result"] is None


def test_search_is_distinct_from_link_intake_permission(conn, config, context):
    config.raw["project_integration"].pop("search_spaces")
    config.raw["project_integration"]["intake_spaces"] = [
        {"simple_name": "k3", "project_key": "space", "type_keys": ["issue"]}
    ]
    assert search.options(conn, config, actor="owner")["available"] is False
    with pytest.raises(PermissionError):
        search.enqueue(conn, config, **context)


def test_next_page_freezes_filter_scope_and_original_expiry(conn, config, context):
    config.raw["project_integration"]["search_spaces"][0]["allowed_item_ids"] = None
    first = search.enqueue(conn, config, **context)
    done = run(conn, config, QueryClient(ids=range(1, 51)))
    second = search.next_page(
        conn, config, actor="owner", search_id=first["search_id"], request_id="next"
    )
    assert second["expires_at"] == first["expires_at"]
    fake = QueryClient(ids=[51])
    result = run(conn, config, fake)
    assert result["state"] == "succeeded"
    assert fake.calls[-1][2] == {"keyword": "", "after_id": 50}
    assert (
        search.next_page(
            conn, config, actor="owner", search_id=first["search_id"], request_id="next"
        )
        == result
    )
    with pytest.raises(ValueError):
        search.next_page(
            conn, config, actor="owner", search_id=result["search_id"], request_id="end"
        )
    config.raw["project_integration"]["search_spaces"][0]["allowed_item_ids"] = [
        123,
        456,
    ]
    with pytest.raises(PermissionError):
        search.next_page(
            conn,
            config,
            actor="owner",
            search_id=done["search_id"],
            request_id="changed",
        )


def test_old_reader_cannot_overwrite_reclaimed_lease(conn, config, context):
    search.enqueue(conn, config, **context)

    def steal():
        conn.execute("UPDATE project_search_requests SET lease_token='new-owner'")

    result = run(conn, config, QueryClient(steal))
    assert result["state"] == "running" and result["result"] is None
    assert (
        conn.execute("SELECT lease_token FROM project_search_requests").fetchone()[0]
        == "new-owner"
    )


def test_reclaim_limit_and_capacity_are_bounded(conn, config, context):
    for n in range(5):
        search.enqueue(conn, config, **(context | {"request_id": str(n)}))
    with pytest.raises(BugConflict):
        search.enqueue(conn, config, **(context | {"request_id": "six"}))
    conn.execute("UPDATE project_search_requests SET attempt=3")
    result = run(conn, config)
    assert (
        result["state"] == "failed" and result["error_code"] == "interrupted_read_limit"
    )


def test_shared_service_routes_search_and_controls_reject_untrusted_fields(
    conn, config, context, monkeypatch
):
    value = execute(
        conn,
        config,
        action="search",
        payload={k: v for k, v in context.items() if k != "actor"},
    )
    assert (
        execute(
            conn,
            config,
            action="search-status",
            payload={"search_id": value["search_id"]},
        )
        == value
    )
    for key in ("actor", "mql", "session_id", "after_id", "profile"):
        with pytest.raises(ValueError):
            execute(
                conn,
                config,
                action="search",
                payload={k: v for k, v in context.items() if k != "actor"}
                | {key: "bad"},
            )
    monkeypatch.setattr(search, "run_one", lambda *a, **kw: {"route": "search"})
    assert service.run_one(conn, lambda: config) == {"route": "search"}


def test_search_config_validates_without_enabling_by_default(config, context):
    validate_config(config.raw)
    config.raw["project_integration"]["search_spaces"][0]["allowed_item_ids"] = []
    with pytest.raises(ConfigError):
        validate_config(config.raw)


def test_http_search_authenticates_actor_and_csrf(console, conn, config, context):
    http, _ = console
    body = {k: v for k, v in context.items() if k != "actor"}
    assert http("/api/project-bugs/search", body)[0] == 403
    cookie, csrf = login(http)
    assert http("/api/project-bugs/search", body, cookie=cookie)[0] == 403
    assert (
        http(
            "/api/project-bugs/search", body | {"mql": "bad"}, cookie=cookie, csrf=csrf
        )[0]
        == 409
    )
    code, _, result = http("/api/project-bugs/search", body, cookie=cookie, csrf=csrf)
    assert code == 200 and result["state"] == "queued"
    done = run(conn, config)
    assert (
        http(
            "/api/project-bugs/search-status",
            {"search_id": result["search_id"]},
            cookie=cookie,
            csrf=csrf,
        )[2]
        == done
    )


def test_expired_root_authorization_stops_before_network(conn, config, context):
    from k3_support.timeutil import observed_clock

    queued = search.enqueue(conn, config, **context)
    fake = QueryClient()
    with observed_clock(utc_now() + timedelta(minutes=31)):
        assert (
            search.enqueue(conn, config, **context)["search_id"] == queued["search_id"]
        )
        done = run(conn, config, fake)
        assert done["state"] == "blocked" and done["authorization_expired"]
    assert fake.calls == []


def test_global_mode_roundtrip_fences_queued_read(conn, config, context):
    search.enqueue(conn, config, **context)
    conn.execute("UPDATE global_control_state SET revision=revision+2")
    fake = QueryClient()
    assert run(conn, config, fake)["state"] == "blocked"
    assert fake.calls == []


@pytest.mark.parametrize("field", ["data", "list", "search_status_info"])
def test_empty_response_requires_verified_envelope(field):
    payload = raw([])
    del payload[field]
    with pytest.raises(ProjectReadError):
        normalize(payload, SCOPE)


def test_search_url_alias_never_changes_compiled_canonical_type(conn, config, context):
    configured = config.raw["project_integration"]["search_spaces"][0]
    configured.update(type_key="internal_type", url_type_key="software_issue")
    params = compile_query(configured)
    assert (
        "`space`.`internal_type`" in params["mql"]
        and "software_issue" not in params["mql"]
    )
    search.enqueue(conn, config, **(context | {"type_key": "internal_type"}))
    result = run(conn, config)
    assert result["state"] == "succeeded"
    assert (
        result["result"]["items"][0]["url"]
        == "https://project.feishu.cn/k3/software_issue/detail/123"
    )
    configured["url_type_key"] = "../bad"
    with pytest.raises(ValueError):
        compile_query(configured)
