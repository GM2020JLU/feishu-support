# ruff: noqa: F811 -- shared isolated fixtures
import copy
import json
import os
import threading
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml
from test_gui import console, login  # noqa: F401
from test_project_read_snapshot import Client, bound  # noqa: F401

from k3_support import project_bug_grants as grants
from k3_support import project_bugs as bugs
from k3_support import project_refresh as refresh
from k3_support import project_refresh_service as service
from k3_support.config import ConfigError, validate_config
from k3_support.project_read_client import ProjectReadError
from k3_support.runtime_control import ensure_global_state
from k3_support.timeutil import utc_now

READER = {
    "enabled": True,
    "executable": "/opt/protected/meegle",
    "sha256": "a" * 64,
    "profile": "dedicated",
    "host": "project.feishu.cn",
}


@pytest.fixture
def refresh_args(conn, config, bound):
    bug, args = bound
    config.raw["identity"]["control_operator_id"] = "owner"
    config.raw["project_integration"] = {
        "write_enabled": False,
        "reader": copy.deepcopy(READER),
    }
    ensure_global_state(conn, actor_id="owner", source="test", external_id="init")
    return {
        "bug_id": bug["bug_id"],
        "actor": "owner",
        "grant_id": args["grant_id"],
        "request_id": "refresh-one",
    }


def run(conn, config, client=None):
    return refresh.run_one(
        conn, lambda: config, client_factory=lambda _: client or Client()
    )


def test_queue_success_replay_and_scope_visibility(conn, config, refresh_args):
    queued = refresh.enqueue(conn, config, **refresh_args)
    assert refresh.enqueue(conn, config, **refresh_args) == queued
    with pytest.raises(bugs.BugConflict):
        refresh.enqueue(conn, config, **(refresh_args | {"request_id": "second"}))
    with pytest.raises(ValueError):
        refresh.status(conn, refresh_id=queued["refresh_id"], actor="other")
    client = Client()
    result = run(conn, config, client)
    assert result["state"] == "succeeded" and result["attempt"] == 1
    assert len(client.calls) == 4
    assert "lease_token" not in result and "reader_digest" not in result
    assert refresh.enqueue(conn, config, **refresh_args) == result
    assert run(conn, config) == {"state": "idle"}
    assert bugs.detail(conn, refresh_args["bug_id"])["snapshot"]["sequence"] == 1


@pytest.mark.parametrize(
    "kind", ["disabled", "profile", "operator", "mode", "revoke", "pause_resume"]
)
def test_configuration_and_authority_changes_block_queued_read_before_client(
    conn, config, refresh_args, kind
):
    refresh.enqueue(conn, config, **refresh_args)
    if kind == "disabled":
        config.raw["project_integration"]["reader"]["enabled"] = False
    if kind == "profile":
        config.raw["project_integration"]["reader"]["profile"] = "another"
    if kind == "operator":
        config.raw["identity"]["control_operator_id"] = "another"
    if kind == "mode":
        config.raw["mode"] = "drain"
    if kind == "revoke":
        grants.revoke(conn, grant_id=refresh_args["grant_id"], actor="owner")
    if kind == "pause_resume":
        conn.execute("UPDATE global_control_state SET revision=revision+2")

    def forbidden(_):
        raise AssertionError("must not construct client")

    result = refresh.run_one(conn, lambda: config, client_factory=forbidden)
    assert result["state"] == "blocked"
    assert conn.execute("SELECT count(*) FROM project_bug_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kind",
    ["lease_expired", "new_consumer", "revoked", "mode_roundtrip", "reader_changed"],
)
def test_late_response_cannot_persist_after_fence_changes(
    conn, config, refresh_args, kind
):
    queued = refresh.enqueue(conn, config, **refresh_args)

    def change(n):
        if n != 4:
            return
        if kind == "lease_expired":
            conn.execute(
                "UPDATE project_refresh_requests SET lease_expires_at=?",
                ((utc_now() - timedelta(seconds=1)).isoformat(),),
            )
        if kind == "new_consumer":
            conn.execute("UPDATE project_refresh_requests SET lease_token='new-owner'")
        if kind == "revoked":
            grants.revoke(conn, grant_id=refresh_args["grant_id"], actor="owner")
        if kind == "mode_roundtrip":
            conn.execute("UPDATE global_control_state SET revision=revision+2")
        if kind == "reader_changed":
            config.raw["project_integration"]["reader"]["profile"] = "new"

    result = run(conn, config, Client(hook=change))
    assert result["state"] == ("running" if kind == "new_consumer" else "blocked")
    assert bugs.detail(conn, refresh_args["bug_id"])["snapshot"] is None
    assert result["refresh_id"] == queued["refresh_id"]


def test_crash_after_snapshot_is_reconciled_without_a_second_read(
    conn, config, refresh_args, monkeypatch
):
    queued = refresh.enqueue(conn, config, **refresh_args)
    original = refresh._finish

    def crash(*args, **kwargs):
        raise SystemExit("simulated crash")

    monkeypatch.setattr(refresh, "_finish", crash)
    with pytest.raises(SystemExit):
        run(conn, config)
    assert bugs.detail(conn, refresh_args["bug_id"])["snapshot"]["sequence"] == 1
    conn.execute(
        "UPDATE project_refresh_requests SET lease_expires_at=?",
        ((utc_now() - timedelta(seconds=1)).isoformat(),),
    )
    monkeypatch.setattr(refresh, "_finish", original)

    def forbidden(_):
        raise AssertionError("recovery must not query Project")

    result = refresh.run_one(conn, lambda: config, client_factory=forbidden)
    assert (
        result["state"] == "succeeded" and result["refresh_id"] == queued["refresh_id"]
    )
    assert conn.execute("SELECT count(*) FROM project_bug_snapshots").fetchone()[0] == 1


def test_expired_reads_reclaim_but_have_a_finite_attempt_budget(
    conn, config, refresh_args
):
    refresh.enqueue(conn, config, **refresh_args)
    conn.execute(
        "UPDATE project_refresh_requests SET state='running',attempt=2,lease_token='old',lease_expires_at=?",
        ((utc_now() - timedelta(seconds=1)).isoformat(),),
    )
    pending = conn.execute(
        "SELECT * FROM project_refresh_requests WHERE state='running'"
    ).fetchone()
    assert refresh.projection(pending)["lease_expired"] is True
    assert run(conn, config)["attempt"] == 3
    refresh.enqueue(conn, config, **(refresh_args | {"request_id": "again"}))
    conn.execute(
        "UPDATE project_refresh_requests SET state='running',attempt=3,lease_token='old',lease_expires_at=? WHERE state='queued'",
        ((utc_now() - timedelta(seconds=1)).isoformat(),),
    )
    result = run(conn, config)
    assert (
        result["state"] == "failed" and result["error_code"] == "interrupted_read_limit"
    )


def test_lease_expiring_during_configuration_check_cannot_be_renewed(
    conn, config, refresh_args
):
    refresh.enqueue(conn, config, **refresh_args)
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        if calls == 2:
            conn.execute(
                "UPDATE project_refresh_requests SET lease_expires_at=?",
                ((utc_now() - timedelta(seconds=1)).isoformat(),),
            )
        return config

    client = Client()
    result = refresh.run_one(conn, loader, client_factory=lambda _: client)
    assert result["state"] == "blocked" and not client.calls
    assert bugs.detail(conn, refresh_args["bug_id"])["snapshot"] is None


def test_read_error_is_terminal_and_does_not_expose_upstream_text(
    conn, config, refresh_args
):
    refresh.enqueue(conn, config, **refresh_args)
    client = Client([ProjectReadError("PRIVATE_UPSTREAM_DETAIL")])
    result = run(conn, config, client)
    assert result["state"] == "failed" and result["error_code"] == "project_read_failed"
    assert "PRIVATE" not in str(result)
    assert run(conn, config) == {"state": "idle"}


def test_refresh_grant_chooser_compares_instants_not_iso_text(
    conn, config, refresh_args
):
    row = conn.execute(
        "SELECT scope_json FROM project_bug_grants WHERE grant_id=?",
        (refresh_args["grant_id"],),
    ).fetchone()
    offset_expiry = (
        (utc_now() + timedelta(hours=1))
        .astimezone(timezone(timedelta(hours=-10)))
        .isoformat()
    )
    grant = grants.issue(
        conn,
        actor="owner",
        request_id="offset-grant",
        scope=json.loads(row[0]),
        expires_at=offset_expiry,
    )
    result = refresh.controls(
        conn, config, bugs._bug(conn, refresh_args["bug_id"]), "owner"
    )
    assert grant["grant_id"] in {g["grant_id"] for g in result["grants"]}


def test_refresh_http_is_csrf_bound_and_rejects_client_selection(
    console, conn, config, refresh_args
):
    http, _ = console
    body = {k: v for k, v in refresh_args.items() if k != "actor"}
    assert http("/api/project-bugs/refresh", body)[0] == 403
    cookie, csrf = login(http)
    assert http("/api/project-bugs/refresh", body, cookie=cookie)[0] == 403
    assert (
        http(
            "/api/project-bugs/refresh",
            body | {"reader": READER},
            cookie=cookie,
            csrf=csrf,
        )[0]
        == 409
    )
    code, _, queued = http("/api/project-bugs/refresh", body, cookie=cookie, csrf=csrf)
    assert code == 200 and queued["state"] == "queued"
    detail = http(
        "/api/project-bugs/detail", {"bug_id": body["bug_id"]}, cookie=cookie, csrf=csrf
    )[2]
    assert detail["refresh_control"]["available"]
    assert detail["refresh_control"]["grants"][0]["grant_id"] == body["grant_id"]
    assert "/opt/protected" not in str(detail)
    run(conn, config)
    result = http(
        "/api/project-bugs/refresh-status",
        {"refresh_id": queued["refresh_id"]},
        cookie=cookie,
        csrf=csrf,
    )[2]
    assert result["state"] == "succeeded"


@pytest.mark.parametrize(
    "change",
    [
        {"token": "secret"},
        {"host": "https://project.feishu.cn"},
        {"enabled": "true"},
        {"executable": "relative"},
        {"sha256": "x"},
        {"profile": "../../personal"},
    ],
)
def test_reader_config_rejects_credentials_and_untrusted_selection(config, change):
    data = copy.deepcopy(config.raw)
    data["project_integration"] = {"write_enabled": False, "reader": READER | change}
    with pytest.raises(ConfigError):
        validate_config(data)


def test_disabled_default_and_valid_opt_in_do_not_enable_writes(config):
    assert "project_integration" not in config.raw
    data = copy.deepcopy(config.raw)
    data["project_integration"] = {"write_enabled": False, "reader": READER}
    assert validate_config(data)["project_integration"] == data["project_integration"]


def test_service_requires_private_existing_storage_and_independent_identity(
    conn, config, tmp_path, monkeypatch
):
    home = tmp_path / "control-home"
    home.mkdir(mode=0o700)
    (home / ".meegle").mkdir(mode=0o700)
    monkeypatch.setattr(service.Path, "home", lambda: home)
    monkeypatch.setattr(
        service.pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(home))
    )
    config.path.write_text(yaml.safe_dump(config.raw))
    config.path.chmod(0o600)
    calls = []
    monkeypatch.setattr(
        service,
        "run_one",
        lambda db, loader, **kw: calls.append(loader()) or {"state": "idle"},
    )
    options = {
        "worker_uid": os.geteuid() + 1,
        "watch": False,
        "stop_event": threading.Event(),
    }
    assert service.run(config.path, **options) == {"state": "idle"}
    assert len(calls) == 1
    with pytest.raises(ValueError):
        service.run(config.path, **(options | {"worker_uid": os.geteuid()}))
    config.path.chmod(0o644)
    with pytest.raises(ValueError):
        service.run(config.path, **options)
    config.path.chmod(0o600)
    # A root-style shared /etc parent must never be group/other-writable.
    # (The permissive-but-protected 755 case needs a separate database
    # directory; it is exercised by the isolated deployment itself.)
    config.path.parent.chmod(0o775)
    with pytest.raises(ValueError, match="protected"):
        service.run(config.path, **options)
    config.path.parent.chmod(0o700)
    conn.execute("DELETE FROM schema_migrations WHERE version=112")
    with pytest.raises(ValueError, match="migration"):
        service.run(config.path, **options)
