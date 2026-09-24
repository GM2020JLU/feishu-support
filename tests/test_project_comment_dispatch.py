# ruff: noqa: F811 -- shared isolated fixtures
import copy
import json
import sqlite3
from datetime import timedelta

import pytest
from test_project_bug_operations import setup  # noqa: F401
from test_project_comment_transport import prepare
from test_project_refresh import READER

from k3_support import project_bug_grants as grants
from k3_support import project_bug_operations as operations
from k3_support import project_comment_dispatch as dispatch
from k3_support.config import ConfigError, validate_config
from k3_support.project_bug_controls import execute
from k3_support.timeutil import utc_now

ACCEPTED_SHA256 = "ffdc86254fe40962efc1b83b7c4f098694b76d9ba92bf92eaef46cf0f5d78362"


def ready(conn, setup, monkeypatch, *, accepted=True):
    cfg, op, _transport, native = prepare(
        conn, setup, creator="dedicated-user"
    )
    cfg.raw["project_integration"]["reader"] = dict(READER) | {
        "sha256": ACCEPTED_SHA256 if accepted else "a" * 64
    }
    cfg.raw["project_integration"]["comment_writer"] = {
        "enabled": True,
        "user_key": "dedicated-user",
    }
    original = native.read_page
    native.subject = "dedicated-user"
    native.identity_checks = 0

    def read(command, params):
        if command == "user.me":
            native.identity_checks += 1
            return {
                "host": native.host,
                "command": command,
                "payload": {"user_key": native.subject},
            }
        return original(command, params)

    native.read_page = read
    args = {
        "operation_id": op["operation_id"],
        "expected_digest": op["request_digest"],
        "request_id": "send-one",
    }
    return cfg, op, args, native


def test_scoped_comment_queue_uses_existing_grant_and_confirms_exact_receipt(
    conn, setup, monkeypatch
):
    cfg, _op, args, native = ready(conn, setup, monkeypatch)
    grant_count = conn.execute("SELECT count(*) FROM project_bug_grants").fetchone()[0]
    queued = execute(conn, cfg, action="send-comment", payload=args)
    result = dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)
    assert (
        result["state"] == "succeeded"
        and result["result"]["operation_state"] == "confirmed"
    )
    assert len(native.writes) == 1 and native.identity_checks == 2
    assert (
        conn.execute("SELECT count(*) FROM project_bug_grants").fetchone()[0]
        == grant_count
    )
    assert (
        execute(conn, cfg, action="send-comment", payload=args)["dispatch_id"]
        == queued["dispatch_id"]
    )
    assert (
        dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)["state"]
        == "idle"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE project_comment_dispatch_requests SET request_digest='changed'"
        )


def test_unaccepted_official_contract_cannot_be_enabled_by_writer_config(
    conn, setup, monkeypatch
):
    cfg, op, args, native = ready(conn, setup, monkeypatch, accepted=False)
    assert not dispatch.controls(
        conn, cfg, actor="owner", operation_id=op["operation_id"]
    )["available"]
    with pytest.raises(dispatch.DispatchBlocked, match="native_contract_unverified"):
        execute(conn, cfg, action="send-comment", payload=args)
    assert (
        native.writes == []
        and conn.execute(
            "SELECT count(*) FROM project_comment_dispatch_requests"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "change",
    [
        {"expected_digest": "stale"},
        {"operation_id": "wrong"},
        {"create_contract_verified": True},
    ],
)
def test_browser_cannot_override_intent_or_contract(conn, setup, monkeypatch, change):
    cfg, _op, args, native = ready(conn, setup, monkeypatch)
    with pytest.raises((ValueError, RuntimeError)):
        execute(conn, cfg, action="send-comment", payload=args | change)
    assert native.writes == []


def test_grant_revocation_after_enqueue_prevents_write(conn, setup, monkeypatch):
    cfg, op, args, native = ready(conn, setup, monkeypatch)
    execute(conn, cfg, action="send-comment", payload=args)
    grants.revoke(conn, actor="owner", grant_id=op["grant_id"])
    result = dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)
    assert result["state"] == "blocked" and native.writes == []
    assert operations._operation(conn, op["operation_id"])["state"] == "prepared"


def test_wrong_dedicated_identity_blocks_before_dispatch(conn, setup, monkeypatch):
    cfg, op, args, native = ready(conn, setup, monkeypatch)
    execute(conn, cfg, action="send-comment", payload=args)
    native.subject = "other-user"
    result = dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)
    assert result["error_code"] == "writer_identity_changed" and native.writes == []
    assert operations._operation(conn, op["operation_id"])["state"] == "prepared"


def test_configuration_change_after_enqueue_blocks_even_if_grant_still_valid(
    conn, setup, monkeypatch
):
    cfg, _op, args, native = ready(conn, setup, monkeypatch)
    execute(conn, cfg, action="send-comment", payload=args)
    cfg.raw["project_integration"]["comment_writer"]["user_key"] = "changed"
    result = dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)
    assert (
        result["state"] == "blocked"
        and native.identity_checks == 0
        and native.writes == []
    )


def test_stolen_lease_during_preflight_never_dispatches(conn, setup, monkeypatch):
    cfg, op, args, native = ready(conn, setup, monkeypatch)
    queued = execute(conn, cfg, action="send-comment", payload=args)

    def hook(number):
        if number == 1:
            conn.execute(
                "UPDATE project_comment_dispatch_requests SET lease_token='replacement' WHERE dispatch_id=?",
                (queued["dispatch_id"],),
            )

    native.hook = hook
    dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)
    assert (
        native.writes == []
        and operations._operation(conn, op["operation_id"])["state"] == "prepared"
    )


def test_crashed_dispatched_operation_is_never_sent_again(conn, setup, monkeypatch):
    cfg, op, args, native = ready(conn, setup, monkeypatch)
    queued = execute(conn, cfg, action="send-comment", payload=args)

    def crash(*args):
        native.writes.append(args)
        raise KeyboardInterrupt()

    native.create_comment = crash
    with pytest.raises(KeyboardInterrupt):
        dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)
    assert operations._operation(conn, op["operation_id"])["state"] == "dispatched"
    conn.execute(
        "UPDATE project_comment_dispatch_requests SET lease_expires_at=? WHERE dispatch_id=?",
        ((utc_now() - timedelta(seconds=1)).isoformat(), queued["dispatch_id"]),
    )

    def forbidden(_):
        raise AssertionError("recovery must not construct a sender")

    result = dispatch.run_one(conn, lambda: cfg, client_factory=forbidden)
    assert (
        result["result"]["reconcile_required"]
        and not result["result"]["dispatch_started_here"]
    )
    assert len(native.writes) == 1


def test_unknown_native_outcome_does_not_retry(conn, setup, monkeypatch):
    cfg, _op, args, native = ready(conn, setup, monkeypatch)
    native.response = TimeoutError("private failure")
    execute(conn, cfg, action="send-comment", payload=args)
    result = dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)
    assert (
        result["state"] == "succeeded"
        and result["result"]["operation_state"] == "unknown"
    )
    assert result["result"]["reconcile_required"] and len(native.writes) == 1
    assert "private failure" not in json.dumps(result)
    with pytest.raises(operations.BugConflict):
        execute(
            conn, cfg, action="send-comment", payload=args | {"request_id": "again"}
        )


def test_writer_config_requires_explicit_identity_and_rejects_escape_flags(config):
    raw = copy.deepcopy(config.raw)
    raw["project_integration"] = {
        "write_enabled": False,
        "comment_writer": {"enabled": True, "user_key": "u"},
    }
    assert (
        validate_config(raw)["project_integration"]["comment_writer"]["user_key"] == "u"
    )
    raw["project_integration"]["comment_writer"]["create_contract_verified"] = True
    with pytest.raises(ConfigError):
        validate_config(raw)


def test_slow_comment_baseline_does_not_relabel_an_old_snapshot_as_fresh(
    conn, setup, monkeypatch
):
    from k3_support.project_read_snapshot import SnapshotReader

    cfg, op, args, native = ready(conn, setup, monkeypatch)
    execute(conn, cfg, action="send-comment", payload=args)
    original = SnapshotReader.collect

    def stale(self, destination):
        result = original(self, destination)
        result["observed_at"] = (utc_now() - timedelta(seconds=61)).isoformat()
        return result

    monkeypatch.setattr(SnapshotReader, "collect", stale)
    result = dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)
    assert result["state"] == "blocked" and native.writes == []
    assert operations._operation(conn, op["operation_id"])["state"] == "prepared"
