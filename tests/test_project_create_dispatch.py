"""Synchronous native creation dispatch keeps draft custody honest."""

# ruff: noqa: F811 -- shared isolated fixtures
import copy
import hashlib
import json
import subprocess
from datetime import timedelta

import pytest
from test_project_bug_create import conn, open_db  # noqa: F401
from test_project_read_client import binary  # noqa: F401
from test_project_refresh import READER

from k3_support import project_bug_create as drafts
from k3_support import project_create_dispatch as dispatch
from k3_support import project_create_grants as grants
from k3_support import project_field_writer_config
from k3_support.config import Config, validate_config
from k3_support.ids import canonical_json
from k3_support.project_bug_controls import execute
from k3_support.project_bugs import BugConflict
from k3_support.project_create_dispatch import CreateBlocked, CreateClient, _ack
from k3_support.project_read_client import ProjectReadError
from k3_support.timeutil import utc_now

SCOPE = {"host": "project.feishu.cn", "project_key": "space", "type_key": "bug"}
URL = "https://project.feishu.cn/space/bug/detail/7123456789"


def ready_draft(conn, cfg, request_id="draft-1"):
    grant = grants.issue(
        conn,
        actor="owner",
        request_id=f"grant-{request_id}",
        scope=SCOPE | {"max_creations": 5},
        expires_at=(utc_now() + timedelta(hours=1)).isoformat(),
    )
    draft = drafts.prepare(
        conn,
        actor="owner",
        request_id=request_id,
        grant_id=grant["grant_id"],
        field_values={"name": "Boot hang", "priority": {"value": "2"}},
        required_fields=[],
        **SCOPE,
    )
    from k3_support import project_bug_search as search
    observed = search.enqueue(conn, cfg, actor="owner", simple_name="space",
                              type_key="bug", keyword="boot", request_id="query-"+request_id)
    conn.execute("UPDATE project_search_requests SET state='succeeded',result_json=? WHERE search_id=?",
                 (canonical_json({"host": SCOPE["host"], "items": [], "next_after_id": None}), observed["search_id"]))
    drafts.attach_duplicates(
        conn,
        draft_id=draft["draft_id"],
        actor="owner",
        search_id=observed["search_id"],
        candidates=[],
    )
    drafts.confirm_not_duplicate(
        conn,
        draft_id=draft["draft_id"],
        actor="owner",
        expected_digest=draft["request_digest"],
    )
    return drafts.mark_ready(
        conn,
        draft_id=draft["draft_id"],
        actor="owner",
        expected_digest=draft["request_digest"],
    )


@pytest.fixture
def cfg(config, monkeypatch):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["identity"]["control_operator_id"] = "owner"
    raw["project_integration"] = {
        "write_enabled": True,
        "reader": dict(READER),
        "search_spaces": [{"simple_name":"space", "project_key":"space", "type_key":"bug", "allowed_item_ids":None}],
        "comment_writer": {"enabled": True, "user_key": "dedicated-user"},
    }
    monkeypatch.setattr(
        dispatch,
        "ACCEPTED_WORKITEM_CREATE_CLIENTS",
        frozenset({READER["sha256"]}),
    )
    return Config(validate_config(raw), config.path)


class FakeCreator:
    def __init__(self, response=None, subject="dedicated-user"):
        self.response = response if response is not None else {
            "url": URL,
            "work_item_id": 7123456789,
        }
        self.subject, self.creates, self.identity_checks = subject, [], 0

    def read_page(self, command, params):
        if command == "workitem.meta-create-fields":
            assert params == {"project_key": "space", "work_item_type": "bug"}
            return {"host": SCOPE["host"], "command": command,
                    "payload": {"FieldConfList": [{"field_key": "name", "is_required": 1}]}}
        assert command == "user.me"
        self.identity_checks += 1
        return {
            "host": "project.feishu.cn",
            "command": command,
            "payload": {"user_key": self.subject},
        }

    def create_workitem(self, scope, field_values):
        self.creates.append((copy.deepcopy(scope), copy.deepcopy(field_values)))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def send(conn, cfg, draft, client):
    return dispatch.send(
        conn,
        cfg,
        draft_id=draft["draft_id"],
        actor="owner",
        expected_digest=draft["request_digest"],
        client_factory=lambda reader: client,
    )


def test_create_client_issues_exactly_the_accepted_argv(binary):
    calls, responses = [], [(0, {"url": URL, "work_item_id": 7123456789})]

    def runner(argv, **kwargs):
        calls.append(list(argv))
        code, value = responses.pop(0)
        return subprocess.CompletedProcess(argv, code, json.dumps(value), "")

    client = CreateClient(
        executable=binary,
        sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        profile="assistant-k3",
        host="project.feishu.cn",
        runner=runner,
    )
    response = client.create_workitem(
        SCOPE, {"priority": {"value": "2"}, "name": "Boot hang"}
    )
    assert response == {"url": URL, "work_item_id": 7123456789}
    tail = calls[0][3:-2]
    assert tail[:3] == ["workitem", "create", "--params"]
    assert json.loads(tail[3]) == {
        "project_key": "space",
        "work_item_type": "bug",
        "fields": [
            {"field_key": "name", "field_value": "Boot hang"},
            {"field_key": "priority", "field_value": canonical_json({"value": "2"})},
        ],
    }
    with pytest.raises(ProjectReadError, match="invalid_create_scope"):
        client.create_workitem(SCOPE | {"host": "evil.example"}, {"name": "x"})
    with pytest.raises(ProjectReadError, match="invalid_create_scope"):
        client.create_workitem({"host": "project.feishu.cn"}, {"name": "x"})
    with pytest.raises(ProjectReadError, match="invalid_create_fields"):
        client.create_workitem(SCOPE, {})


@pytest.mark.parametrize(
    "response",
    [
        None,
        "success",
        {"url": URL},
        {"url": URL, "work_item_id": "7123456789"},
        {"url": URL, "work_item_id": True},
        {"url": URL, "work_item_id": 7123456789, "extra": 1},
        {"url": URL.replace("space", "other"), "work_item_id": 7123456789},
        {"url": URL.replace("7123456789", "999"), "work_item_id": 7123456789},
        {"url": "https://evil.example/space/bug/detail/7123456789", "work_item_id": 7123456789},
    ],
)
def test_ack_refuses_everything_but_the_bound_url(response):
    assert _ack(SCOPE, response) is None


def test_ack_accepts_only_the_exactly_bound_creation():
    assert _ack(SCOPE, {"url": URL, "work_item_id": 7123456789}) == "7123456789"


def test_send_settles_created_and_consumes_single_dispatch(conn, cfg):
    draft = ready_draft(conn, cfg)
    client = FakeCreator()
    settled = dispatch.send(
        conn,
        cfg,
        draft_id=draft["draft_id"],
        actor="owner",
        expected_digest=draft["request_digest"],
        client_factory=lambda reader: client,
    )
    assert settled["state"] == "created"
    assert settled["created_item_id"] == "7123456789"
    assert client.identity_checks == 1
    assert client.creates == [
        (SCOPE, {"name": "Boot hang", "priority": {"value": "2"}})
    ]
    events = conn.execute(
        "SELECT kind FROM project_create_grant_events ORDER BY created_at"
    ).fetchall()
    assert [row["kind"] for row in events][-1] == "creation_settled"
    with pytest.raises(BugConflict, match="not ready"):
        send(conn, cfg, draft, client)


def test_wrong_identity_blocks_before_consuming_the_draft(conn, cfg):
    draft = ready_draft(conn, cfg)
    client = FakeCreator(subject="somebody-else")
    with pytest.raises(CreateBlocked, match="writer_identity_changed"):
        send(conn, cfg, draft, client)
    assert client.creates == []
    row = drafts._owned(conn, draft["draft_id"], "owner")
    assert row["state"] == "ready"


def test_provider_exception_settles_unknown_without_retry(conn, cfg):
    draft = ready_draft(conn, cfg)
    client = FakeCreator(response=ProjectReadError("provider_unavailable"))
    settled = send(conn, cfg, draft, client)
    assert settled["state"] == "unknown"
    assert len(client.creates) == 1
    with pytest.raises(BugConflict, match="not ready"):
        send(conn, cfg, draft, client)
    assert len(client.creates) == 1


def test_provider_rejection_settles_rejected_and_releases_budget(conn, cfg):
    draft = ready_draft(conn, cfg)
    client = FakeCreator(response=dispatch.CreateRejected())
    settled = send(conn, cfg, draft, client)
    assert settled["state"] == "rejected"
    assert settled["error_code"] == "provider_rejected"
    assert len(client.creates) == 1
    # A proven non-creation must not consume the bounded create budget.
    assert grants.remaining_budget(conn, draft["grant_id"]) == 5
    with pytest.raises(BugConflict, match="not ready"):
        send(conn, cfg, draft, client)
    assert len(client.creates) == 1


def test_native_error_envelope_does_not_prove_absence(tmp_path):
    rejection = {"data": None, "error": {"code": "SERVER_CALL_FAILED"}, "meta": {}}
    path = tmp_path / "fake-meegle"
    path.write_text(
        "#!/usr/bin/python3\nprint('''" + json.dumps(rejection) + "''')\n"
    )
    path.chmod(0o700)
    client = dispatch.CreateClient(
        executable=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        profile="default",
        host="project.feishu.cn",
    )
    with pytest.raises(ProjectReadError):
        client.create_workitem(
            {"host": "project.feishu.cn", "project_key": "p", "type_key": "t"},
            {"name": "x"},
        )


def test_unrecognized_acknowledgement_settles_unknown(conn, cfg):
    draft = ready_draft(conn, cfg)
    client = FakeCreator(response={"url": URL, "work_item_id": "7123456789"})
    assert send(conn, cfg, draft, client)["state"] == "unknown"


def test_gates_refuse_before_any_reservation(conn, cfg, monkeypatch):
    draft = ready_draft(conn, cfg)
    client = FakeCreator()
    disabled = copy.deepcopy(cfg.raw)
    disabled["project_integration"]["write_enabled"] = False
    with pytest.raises(CreateBlocked, match="write_provider_unavailable"):
        send(conn, Config(validate_config(disabled), cfg.path), draft, client)
    unaccepted = copy.deepcopy(cfg.raw)
    unaccepted["project_integration"]["reader"]["sha256"] = "b" * 64
    with pytest.raises(CreateBlocked, match="native_contract_unverified"):
        send(conn, Config(validate_config(unaccepted), cfg.path), draft, client)
    silent = copy.deepcopy(cfg.raw)
    del silent["project_integration"]["comment_writer"]
    with pytest.raises(CreateBlocked, match="writer_not_configured"):
        send(conn, Config(validate_config(silent), cfg.path), draft, client)
    assert client.creates == []
    assert drafts._owned(conn, draft["draft_id"], "owner")["state"] == "ready"


def test_controls_dispatch_never_consumes_draft_when_binary_is_missing(conn, cfg):
    draft = ready_draft(conn, cfg)
    with pytest.raises(ProjectReadError, match="client_unavailable"):
        execute(
            conn,
            cfg,
            action="dispatch-create-draft",
            payload={
                "draft_id": draft["draft_id"],
                "expected_digest": draft["request_digest"],
            },
        )
    assert drafts._owned(conn, draft["draft_id"], "owner")["state"] == "ready"
    with pytest.raises(ValueError, match="exact request fields"):
        execute(
            conn,
            cfg,
            action="dispatch-create-draft",
            payload={"draft_id": draft["draft_id"]},
        )


def test_field_writer_closing_status_ids_are_validated():
    base = {
        "enabled": True,
        "user_key": "u",
        "allowed_fields": ["progress"],
        "risk_policy": "forbid",
    }
    assert project_field_writer_config.validate(base)["closing_status_ids"] == []
    ok = project_field_writer_config.validate(base | {"closing_status_ids": ["CLOSED"]})
    assert ok["closing_status_ids"] == ["CLOSED"]
    for bad in (["CLOSED", "CLOSED"], [""], ["x"] * 21, "CLOSED"):
        with pytest.raises(ValueError, match="closing status ids"):
            project_field_writer_config.validate(base | {"closing_status_ids": bad})


@pytest.mark.parametrize('rows', [
    [{'field_key': 'server_added_required', 'is_required': 1}],
    [{'field_key': 'name', 'is_required': True}],
    [{'field_key': 'name', 'is_required': 1}, {'field_key': 'name', 'is_required': 0}],
    [],
])
def test_server_requirements_block_creation_before_reservation(conn, cfg, rows):
    draft = ready_draft(conn, cfg)
    client = FakeCreator()
    original = client.read_page

    def read(command, params):
        if command == 'workitem.meta-create-fields':
            return {'host': SCOPE['host'], 'command': command, 'payload': {'FieldConfList': rows}}
        return original(command, params)

    client.read_page = read
    before = list(conn.iterdump())
    with pytest.raises((ValueError, ProjectReadError)):
        send(conn, cfg, draft, client)
    assert not client.creates
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize('bad',['fabricated','changed_candidates','scope_revoked'])
def test_dispatch_revalidates_old_duplicate_evidence_before_native_reads(conn,cfg,bad):
    draft=ready_draft(conn,cfg)
    if bad=='scope_revoked':
        cfg.raw['project_integration']['search_spaces']=[]
    else:
        drafts.attach_duplicates(conn,actor='owner',draft_id=draft['draft_id'],
                                 search_id='invented' if bad=='fabricated' else draft['duplicate_search_id'],
                                 candidates=[] if bad=='fabricated' else [{'item_id':'99','title':'invented'}])
        drafts.confirm_not_duplicate(conn,actor='owner',draft_id=draft['draft_id'],expected_digest=draft['request_digest'])
        drafts.mark_ready(conn,actor='owner',draft_id=draft['draft_id'],expected_digest=draft['request_digest'])
    client=FakeCreator()
    with pytest.raises((ValueError,PermissionError)):
        send(conn,cfg,draft,client)
    assert client.identity_checks==0 and client.creates==[]
    assert drafts._owned(conn,draft['draft_id'],'owner')['state']=='ready'


def test_duplicate_authority_changed_during_preflight_never_reserves_create(conn,cfg):
    draft=ready_draft(conn,cfg)
    client=FakeCreator()
    original=client.read_page
    def read(command,params):
        result=original(command,params)
        if command=='workitem.meta-create-fields':
            cfg.raw['project_integration']['search_spaces']=[]
        return result
    client.read_page=read
    with pytest.raises(PermissionError):send(conn,cfg,draft,client)
    assert client.creates==[]
    assert drafts._owned(conn,draft['draft_id'],'owner')['state']=='ready'
