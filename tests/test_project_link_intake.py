# ruff: noqa: F811 -- shared isolated fixture
import copy
import json
import sqlite3
from datetime import timedelta

import pytest
from test_gui import console, login  # noqa: F401
from test_project_read_snapshot import Client, responses
from test_project_refresh import READER

from k3_support import project_bugs as bugs
from k3_support import project_link_intake as intake
from k3_support import project_refresh as refresh
from k3_support import project_refresh_service as service
from k3_support.config import ConfigError, validate_config
from k3_support.db import transaction
from k3_support.runtime_control import ensure_global_state
from k3_support.store import create_case
from k3_support.timeutil import iso_now, observed_clock, utc_now

URL = "https://project.feishu.cn/k3/issue/detail/123"
SPACE = {
    "projects": [{"project_key": "space", "simple_name": "k3", "name": "Synthetic"}],
    "pagination": {"has_more": False, "page_num": 1, "page_size": 50, "total": 1},
}


class IntakeClient(Client):
    def __init__(self, values=None, hook=None):
        super().__init__(
            values if values is not None else [copy.deepcopy(SPACE), *responses()],
            hook=hook,
        )
        self.decodes = []

    def decode_workitem_url(self, url):
        self.decodes.append(url)
        return {
            "host": "project.feishu.cn",
            "simple_name": "k3",
            "work_item_type": "issue",
            "work_item_id": "123",
        }


@pytest.fixture
def context(conn, config):
    config.raw["identity"]["control_operator_id"] = "owner"
    config.raw["project_integration"] = {
        "write_enabled": False,
        "reader": copy.deepcopy(READER),
        "intake_spaces": [
            {"simple_name": "k3", "project_key": "space", "type_keys": ["issue"]}
        ],
    }
    ensure_global_state(
        conn, actor_id="owner", source="test", external_id="intake-init"
    )
    return {
        "actor": "owner",
        "url": URL,
        "request_id": "one",
        "read_hours": 8,
        "local_priority": "P2",
    }


def run(conn, config, client=None):
    return intake.run_one(
        conn, lambda: config, client_factory=lambda _: client or IntakeClient()
    )


def test_atomic_link_intake_creates_only_local_read_state_and_deduplicates(
    conn, config, context
):
    queued = intake.enqueue(conn, config, **context)
    assert intake.enqueue(conn, config, **context) == queued
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
    client = IntakeClient()
    result = run(conn, config, client)
    assert result["state"] == "succeeded" and result["reused"] == 0
    assert client.decodes == [URL] and client.calls[0] == (
        "project.search",
        {"project_key": "space", "page_num": 1},
    )
    assert intake.enqueue(conn, config, **context) == result
    assert run(conn, config) == {"state": "idle"}
    bug = bugs.detail(conn, result["bug_id"])
    assert bug["snapshot"]["fields"]["name"] == "Synthetic" and bug["rounds"] == []
    scope = json.loads(
        conn.execute(
            "SELECT scope_json FROM project_bug_grants WHERE grant_id=?",
            (result["grant_id"],),
        ).fetchone()[0]
    )
    assert (
        scope["actions"] == ["bug.read"]
        and scope["bug_ids"] == [result["bug_id"]]
        and scope["fields"] == []
    )
    case = conn.execute("SELECT * FROM cases").fetchone()
    assert (
        case["state"] == "intake"
        and case["severity"] == "P2"
        and case["disclosure_class"] == "private"
    )
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_later_intake_reuses_binding_without_resetting_case_priority(
    conn, config, context
):
    intake.enqueue(conn, config, **context)
    first = run(conn, config)
    intake.enqueue(
        conn, config, **(context | {"request_id": "two", "local_priority": "P0"})
    )
    second = run(conn, config)
    assert second["bug_id"] == first["bug_id"] and second["reused"] == 1
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 1
    assert conn.execute("SELECT severity FROM cases").fetchone()[0] == "P2"
    assert bugs.detail(conn, first["bug_id"])["snapshot"]["sequence"] == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://project.feishu.cn/k3/issue/detail/123",
        "https://other.example/k3/issue/detail/123",
        URL + "?token=private",
        URL + "#",
        URL + "?",
        "https://project.feishu.cn/other/issue/detail/123",
        "https://project.feishu.cn/k3/story/detail/123",
        "https://project.feishu.cn/k3/issue/detail/%31%32%33",
    ],
)
def test_unapproved_or_ambiguous_links_never_queue(conn, config, context, url):
    with pytest.raises((ValueError, PermissionError)):
        intake.enqueue(conn, config, **(context | {"url": url}))
    assert conn.execute("SELECT count(*) FROM project_link_intakes").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kind", ["wrong_key", "wrong_slug", "ambiguous", "truncated", "malformed"]
)
def test_space_resolution_must_match_authoritative_allowlist(
    conn, config, context, kind
):
    intake.enqueue(conn, config, **context)
    space = copy.deepcopy(SPACE)
    if kind == "wrong_key":
        space["projects"][0]["project_key"] = "other"
    if kind == "wrong_slug":
        space["projects"][0]["simple_name"] = "other"
    if kind == "ambiguous":
        space["projects"] *= 2
        space["pagination"]["total"] = 2
    if kind == "truncated":
        space["pagination"]["total"] = 2
    if kind == "malformed":
        space = []
    result = run(conn, config, IntakeClient([space]))
    assert result["state"] == "failed"
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0


@pytest.mark.parametrize("phase", ["grant", "snapshot"])
def test_failure_after_local_creation_rolls_back_every_related_record(
    conn, config, context, monkeypatch, phase
):
    intake.enqueue(conn, config, **context)
    if phase == "grant":
        original = intake.grants.issue

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("after grant")

        monkeypatch.setattr(intake.grants, "issue", fail)
    else:
        original = bugs.observe

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("after snapshot")

        monkeypatch.setattr(bugs, "observe", fail)
    assert run(conn, config)["state"] == "failed"
    for table in [
        "cases",
        "case_events",
        "project_bugs",
        "project_bug_grants",
        "project_bug_grant_events",
        "project_bug_snapshots",
        "project_bug_read_evidence",
    ]:
        assert conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_nested_case_unit_does_not_commit_outer_transaction(conn):
    with pytest.raises(RuntimeError), transaction(conn):
        create_case(
            conn, title="Synthetic", case_type="bug", severity="P2", confidence=1
        )
        raise RuntimeError("rollback outer")
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
    with transaction(conn):
        with pytest.raises(sqlite3.IntegrityError):
            create_case(
                conn, title="Bad", case_type="bug", severity="INVALID", confidence=1
            )
        create_case(conn, title="Good", case_type="bug", severity="P2", confidence=1)
    assert conn.execute("SELECT title FROM cases").fetchone()[0] == "Good"


@pytest.mark.parametrize("kind", ["scope_removed", "mode_roundtrip", "lease_lost"])
def test_late_read_does_not_create_state_after_authority_changes(
    conn, config, context, kind
):
    intake.enqueue(conn, config, **context)

    def change(n):
        if n != 5:
            return
        if kind == "scope_removed":
            config.raw["project_integration"]["intake_spaces"] = []
        if kind == "mode_roundtrip":
            conn.execute("UPDATE global_control_state SET revision=revision+2")
        if kind == "lease_lost":
            conn.execute("UPDATE project_link_intakes SET lease_token='new-owner'")

    result = run(conn, config, IntakeClient(hook=change))
    assert result["state"] == ("running" if kind == "lease_lost" else "blocked")
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0


def test_concurrent_newer_snapshot_is_preserved(conn, config, context):
    intake.enqueue(conn, config, **context)
    first = run(conn, config)
    intake.enqueue(conn, config, **(context | {"request_id": "two"}))

    def newer(n):
        if n != 5:
            return
        bugs.observe(
            conn,
            bug_id=first["bug_id"],
            observation_id="newer",
            expected_sequence=1,
            observed_at=iso_now(),
            payload={
                "fields": {"name": "newer"},
                "status_id": "OPEN",
                "closure": {"closed": None, "reason": None},
                "remote_version": None,
                "schema_digest": None,
            },
        )

    assert run(conn, config, IntakeClient(hook=newer))["state"] == "failed"
    assert bugs.detail(conn, first["bug_id"])["snapshot"]["fields"]["name"] == "newer"
    assert conn.execute("SELECT count(*) FROM project_bug_grants").fetchone()[0] == 1


def test_expired_intake_can_be_replaced_but_replay_does_not_renew_it(
    conn, config, context
):
    first = intake.enqueue(conn, config, **context)
    with observed_clock(utc_now() + timedelta(minutes=31)):
        replay = intake.enqueue(conn, config, **context)
        assert (
            replay["authorization_expired"]
            and replay["grant_expires_at"] == first["grant_expires_at"]
        )
        second = intake.enqueue(conn, config, **(context | {"request_id": "two"}))
        assert second["intake_id"] != first["intake_id"]
        assert (
            intake.status(conn, actor="owner", intake_id=first["intake_id"])["state"]
            == "blocked"
        )


def test_http_intake_uses_authenticated_actor_and_exact_payload(
    console, conn, config, context
):
    http, _ = console
    body = {k: v for k, v in context.items() if k != "actor"}
    assert http("/api/project-bugs/intake-link", body)[0] == 403
    cookie, csrf = login(http)
    assert http("/api/project-bugs/intake-link", body, cookie=cookie)[0] == 403
    assert (
        http(
            "/api/project-bugs/intake-link",
            body | {"project_key": "other"},
            cookie=cookie,
            csrf=csrf,
        )[0]
        == 409
    )
    code, _, result = http(
        "/api/project-bugs/intake-link", body, cookie=cookie, csrf=csrf
    )
    assert code == 200 and result["state"] == "queued"
    result = run(conn, config)
    status = http(
        "/api/project-bugs/intake-status",
        {"intake_id": result["intake_id"]},
        cookie=cookie,
        csrf=csrf,
    )[2]
    assert status["bug_id"] == result["bug_id"]
    assert intake.listing(conn, config, actor="other", after_id="")["items"] == []


def test_worker_dispatches_oldest_eligible_kind(conn, config, context, monkeypatch):
    intake.enqueue(conn, config, **context)
    first = run(conn, config)
    intake.enqueue(conn, config, **(context | {"request_id": "older"}))
    with observed_clock(utc_now() + timedelta(seconds=1)):
        refresh.enqueue(
            conn,
            config,
            bug_id=first["bug_id"],
            actor="owner",
            grant_id=first["grant_id"],
            request_id="newer",
        )
    calls = []
    monkeypatch.setattr(
        intake,
        "run_one",
        lambda *a, **k: calls.append("intake") or {"state": "selected"},
    )
    monkeypatch.setattr(
        service,
        "refresh_one",
        lambda *a, **k: calls.append("refresh") or {"state": "selected"},
    )
    assert service.run_one(conn, lambda: config) == {"state": "selected"}
    assert calls == ["intake"]


def test_scope_config_rejects_duplicate_or_credential_fields(config, context):
    data = copy.deepcopy(config.raw)
    data["project_integration"]["intake_spaces"] *= 2
    with pytest.raises(ConfigError):
        validate_config(data)
    data = copy.deepcopy(config.raw)
    data["project_integration"]["intake_spaces"][0]["token"] = "not_allowed"
    with pytest.raises(ConfigError):
        validate_config(data)


def test_intake_resolves_all_space_pages_before_reading_bug(conn, config, context):
    intake.enqueue(conn, config, **context)
    first = copy.deepcopy(SPACE)
    last = copy.deepcopy(SPACE)
    first["projects"] = [
        {"project_key": f"other-{i}", "simple_name": f"other-{i}"} for i in range(50)
    ]
    first["pagination"].update(has_more=True, total=51)
    last["pagination"].update(page_num=2, total=51)
    client = IntakeClient([first, last, *responses()])
    assert run(conn, config, client)["state"] == "succeeded"
    assert (
        client.calls[1][1]["page_num"] == 2
        and client.calls[2][0] == "workitem.meta-fields"
    )


def test_expired_intake_blocks_before_constructing_any_client(conn, config, context):
    intake.enqueue(conn, config, **context)

    def forbidden(_):
        raise AssertionError("expired authorization must not query")

    with observed_clock(utc_now() + timedelta(minutes=31)):
        result = intake.run_one(conn, lambda: config, client_factory=forbidden)
    assert result["state"] == "blocked"


def test_intake_identity_and_replay_parameters_are_immutable(conn, config, context):
    intake.enqueue(conn, config, **context)
    with pytest.raises(bugs.BugConflict):
        intake.enqueue(conn, config, **(context | {"read_hours": 24}))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE project_link_intakes SET url='https://other.example/'")


def test_intake_listing_has_stable_bounded_cursor(conn, config, context):
    for number in range(21):
        intake.enqueue(
            conn,
            config,
            **(
                context
                | {"request_id": str(number), "url": URL[:-3] + str(number + 100)}
            ),
        )
    first = intake.listing(conn, config, actor="owner", after_id="")
    second = intake.listing(conn, config, actor="owner", after_id=first["next_cursor"])
    assert (
        len(first["items"]) == 20
        and len(second["items"]) == 1
        and second["next_cursor"] is None
    )
    assert first["items"][-1]["intake_id"] < second["items"][0]["intake_id"]


def test_explicit_url_alias_uses_canonical_type_for_schema_binding_and_grant(
    conn, config, context
):
    from k3_support.project_intake_policy import parse

    space = config.raw["project_integration"]["intake_spaces"][0]
    space.update(
        type_keys=["internal_type"], type_aliases={"software_issue": "internal_type"}
    )
    context = context | {
        "url": "https://project.feishu.cn/k3/software_issue/detail/123"
    }
    identity = parse(config, context["url"])
    assert (
        identity["type_key"] == "internal_type"
        and identity["url_type_key"] == "software_issue"
    )
    values = responses()
    for item in values:
        if "work_item_attribute" in item:
            item["work_item_attribute"]["work_item_type"]["key"] = "internal_type"

    class AliasClient(IntakeClient):
        def decode_workitem_url(self, url):
            return super().decode_workitem_url(url) | {
                "work_item_type": "software_issue"
            }

    fake = AliasClient([copy.deepcopy(SPACE), *values])
    intake.enqueue(conn, config, **context)
    result = run(conn, config, fake)
    assert result["state"] == "succeeded"
    assert bugs.detail(conn, result["bug_id"])["type_key"] == "internal_type"
    scope = json.loads(
        conn.execute(
            "SELECT scope_json FROM project_bug_grants WHERE grant_id=?",
            (result["grant_id"],),
        ).fetchone()[0]
    )
    assert scope["type_key"] == "internal_type"
    assert [
        args["work_item_type"]
        for cmd, args in fake.calls
        if cmd == "workitem.meta-fields"
    ] == ["internal_type", "internal_type"]


@pytest.mark.parametrize(
    "aliases",
    [
        {"software_issue": "not-approved"},
        {"issue": "issue"},
        {"../bad": "issue"},
        {"software_issue": None},
        [],
    ],
)
def test_invalid_url_alias_configuration_is_rejected(config, context, aliases):
    config.raw["project_integration"]["intake_spaces"][0]["type_aliases"] = aliases
    with pytest.raises(ConfigError):
        validate_config(config.raw)
