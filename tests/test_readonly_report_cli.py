"""CLI report contracts use isolated SQLite fixtures, never live transports."""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess

import pytest
import yaml
from conftest import config_data

from k3_support import (
    cli,
    db,
    knowledge_gaps,
    notification_snooze,
    readiness,
    recovery_inventory,
    base_sync_inventory,
)
from k3_support.store import create_case, enqueue_outbox, ingest_event

COMMANDS = ("knowledge-gap-report", "readiness-report", "notification-status", "recovery-inventory", "base-recovery-report")
SINCE = "2026-09-01T00:00:00+05:45"
UNTIL = "2026-09-08T00:00:00+05:45"
WHEN = "2026-09-07T09:00:00+00:00"


@pytest.fixture
def report_config(config):
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    return config


@pytest.fixture
def forbid_side_effects(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("read-only reports must not initialize, migrate or use transports")

    for module in (cli, db):
        monkeypatch.setattr(module, "connect", forbidden)
        monkeypatch.setattr(module, "migrate", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def _question(conn, index, text):
    event, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id=f"fixture-message-{index}",
        payload={"text": text},
        occurred_at=WHEN,
    )
    case, _ = create_case(
        conn,
        title=text,
        case_type="faq",
        severity="P2",
        confidence=0.9,
        source_event_pk=event,
    )
    conn.execute(
        """INSERT INTO route_decisions
           (route_decision_id,event_pk,case_id,route,proposed_route,confidence,
            issue_type,severity,domain,requires_owner_judgment,profile_snapshot_json,
            model_output_digest,created_at)
           VALUES(?,?,?,'research','research',0.9,'faq','P2','boot',0,'{}','fixture',?)""",
        (f"fixture-route-{index}", event, case, WHEN),
    )
    return case, event


def _run(config, capsys, command, *args):
    code = cli.main(["--config", str(config.path), command, *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out if code == 0 else captured.err)


@pytest.mark.parametrize("command", COMMANDS)
def test_missing_database_does_not_create_an_instance(
    tmp_path, capsys, command, forbid_side_effects
):
    path = tmp_path / "configuration.yaml"
    data = config_data(tmp_path / "instance-not-created")
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    before = path.read_bytes()
    assert cli.main(["--config", str(path), command]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "FileNotFoundError"
    assert "do not initialize" in error["message"]
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", COMMANDS)
def test_missing_config_is_not_created(tmp_path, capsys, command, forbid_side_effects):
    path = tmp_path / "missing-instance" / "config.yaml"
    assert cli.main(["--config", str(path), command]) == 2
    assert json.loads(capsys.readouterr().err)["ok"] is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("schema", ("older", "future", "absent"))
def test_schema_mismatch_is_reported_without_migration(
    conn, report_config, capsys, command, schema, forbid_side_effects
):
    if schema == "older":
        conn.execute("DELETE FROM schema_migrations WHERE version=(SELECT max(version) FROM schema_migrations)")
    elif schema == "future":
        conn.execute("UPDATE schema_migrations SET version=999 WHERE version=(SELECT max(version) FROM schema_migrations)")
    else:
        conn.execute("DROP TABLE schema_migrations")
    before = conn.serialize()
    config_before = report_config.path.read_bytes()
    code, error = _run(report_config, capsys, command)
    assert code == 2
    assert error["error"] in {"DatabaseError", "OperationalError"}
    assert conn.serialize() == before
    assert report_config.path.read_bytes() == config_before


@pytest.mark.parametrize("command", COMMANDS)
def test_reports_do_not_initialize_controller_or_touch_pending_delivery(
    conn, report_config, capsys, command, forbid_side_effects
):
    case, event = _question(conn, 0, "Pico 风扇怎么调")
    enqueue_outbox(
        conn,
        channel="feishu_im",
        action_type="reply",
        destination="fixture-never-send",
        payload={"text": "fixture pending"},
        idempotency_key="fixture-report-pending",
        case_id=case,
        source_event_pk=event,
    )
    assert conn.execute("SELECT count(*) FROM global_control_state").fetchone()[0] == 0
    before = conn.serialize()
    files_before = set(report_config.data_dir.rglob("*"))
    code, report = _run(report_config, capsys, command)
    assert code == 0
    assert report["read_only"] is True
    assert conn.serialize() == before
    assert set(report_config.data_dir.rglob("*")) == files_before
    if command == "readiness-report":
        assert report["active_ready"] is False
        assert report["activation_performed"] is False
        assert report["authority_boundary"]["global_control"]["state"] == "not_initialized"


def test_gap_cli_fixed_window_pagination_totals_and_digest(
    conn, report_config, capsys, forbid_side_effects
):
    for index, text in enumerate(("UFS 查询介质", "UFS 查询介质", "EC 固件更新", "EC 固件更新")):
        _question(conn, index, text)
    args = ("--since", SINCE, "--until", UNTIL, "--page-size", "1", "--min-repeat", "2")
    before = conn.serialize()
    code, first = _run(report_config, capsys, "knowledge-gap-report", *args, "--page", "1")
    assert code == 0
    code, second = _run(
        report_config, capsys, "knowledge-gap-report", *args,
        "--page", "2", "--expected-digest", first["report_digest"],
    )
    assert code == 0
    assert first["report_digest"] == second["report_digest"]
    assert first["repeated_intents"]["total"] == second["repeated_intents"]["total"] == 2
    assert first["repeated_intents"]["items"][0]["candidate_id"] != second["repeated_intents"]["items"][0]["candidate_id"]
    assert second["repeated_intents"]["has_more"] is False
    assert second["repeated_intents"]["items"][0]["human_truth"] is False
    assert second["coverage"]["knowledge_selected"]["denominator"] == 4
    assert second["outcomes"]["field_resolved"]["count"] is None
    assert conn.serialize() == before
    _question(conn, 4, "新问题")
    changed = conn.serialize()
    code, error = _run(
        report_config, capsys, "knowledge-gap-report", *args,
        "--page", "2", "--expected-digest", first["report_digest"],
    )
    assert code == 2
    assert "snapshot changed" in error["message"]
    assert conn.serialize() == changed


@pytest.mark.parametrize("args", (
    ("--page", "0"), ("--page-size", "101"), ("--min-repeat", "1"),
    ("--since", "2026-09-01T00:00:00"), ("--until", "not-a-date"),
    ("--since", UNTIL, "--until", SINCE), ("--expected-digest", "wrong"),
))
def test_gap_cli_invalid_arguments_leave_database_untouched(
    conn, report_config, capsys, args, forbid_side_effects
):
    before = conn.serialize()
    code, error = _run(report_config, capsys, "knowledge-gap-report", *args)
    assert code == 2 and error["ok"] is False
    assert conn.serialize() == before


def test_readiness_cli_never_infers_actual_runtime_from_a_verified_artifact(
    conn, report_config, capsys, monkeypatch, forbid_side_effects
):
    calls = []

    def verified_artifact(*_args, **kwargs):
        calls.append(kwargs)
        return {
            "artifact_verified": True,
            "ready": False,
            "release_digest": "f" * 64,
            "release_id": "synthetic-only-reporter-test",
            "evidence_class": "human_reviewed",
            "sample_size": 150,
        }

    monkeypatch.setattr(readiness, "verify_release", verified_artifact)
    before = conn.serialize()
    code, report = _run(report_config, capsys, "readiness-report")
    assert code == 0
    assert len(calls) == 1 and "runtime_binding" not in calls[0]
    knowledge = report["knowledge_release"]
    assert knowledge["artifact_verified"] is True
    assert knowledge["state"] == "artifact_verified_current_runtime_unknown"
    assert knowledge["current_runtime_verified"] is False
    assert knowledge["actual_runtime_descriptor_supplied"] is False
    assert knowledge["descriptor_source"] == "not_observed"
    assert report["active_ready"] is False
    assert report["authority_boundary"]["os_isolation_verified"] is False
    assert conn.serialize() == before


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("raise_error", (False, True))
def test_report_connection_enforces_read_only_and_closes_even_on_error(
    conn, report_config, capsys, monkeypatch, command, raise_error, forbid_side_effects
):
    opened = []

    def reader(connection, *_args, **_kwargs):
        opened.append(connection)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM outbox")
        if raise_error:
            raise ValueError("synthetic report failure")
        return {"read_only": True}

    module, function = {
        "knowledge-gap-report": (knowledge_gaps, "knowledge_gap_report"),
        "readiness-report": (readiness, "readiness_report"),
        "notification-status": (notification_snooze, "status"),
        "recovery-inventory": (recovery_inventory, "audit"),
        "base-recovery-report": (base_sync_inventory, "snapshot"),
    }[command]
    monkeypatch.setattr(module, function, reader)
    before = conn.serialize()
    code, report = _run(report_config, capsys, command)
    assert code == (2 if raise_error else 0)
    if raise_error:
        assert report["ok"] is False
    else:
        assert report["read_only"] is True
    assert conn.serialize() == before
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_readiness_cli_does_not_accept_an_unverified_descriptor():
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["readiness-report", "--runtime-binding", "{}"])
    assert exc.value.code == 2
