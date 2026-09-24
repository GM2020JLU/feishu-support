"""Reporter contracts use synthetic observations, never production proof."""

from __future__ import annotations

import copy
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from k3_support import readiness
from k3_support.config import Config
from k3_support.operations import heartbeat


def green_health(conn, config):
    for component in (
        "ingress_poll",
        "ingress_bot",
        "worker",
        "job_worker",
        "outbox",
        "reconcile",
    ):
        heartbeat(conn, component, "ready", {})
    folder = config.data_dir / "backups"
    folder.mkdir()
    with sqlite3.connect(folder / "support-fixture.db") as target:
        conn.backup(target)


def verified_artifact(*, evidence="human_reviewed"):
    # Mock verifier output solely to test report classification; not a Gold label.
    return {
        "ready": False,
        "artifact_verified": True,
        "reason": "current_runtime_not_verified",
        "release_id": "synthetic-report-fixture",
        "release_digest": "a" * 64,
        "evidence_class": evidence,
        "sample_size": 150,
    }


def test_green_services_empty_queues_do_not_imply_active_authority(conn, config):
    green_health(conn, config)
    report = readiness.readiness_report(conn, config)
    assert report["runtime_health"]["state"] == "healthy"
    assert report["runtime_health"]["queue_counts"]["outbox"] == {}
    assert report["authority_boundary"]["state"] == "unproven"
    assert report["knowledge_release"]["state"] == "blocked"
    assert report["active_ready"] is report["activation_performed"] is False
    categories = {blocker["category"] for blocker in report["blockers"]}
    assert categories == {"authority_boundary", "knowledge_release"}


def test_artifact_without_actual_runtime_stays_unknown_and_never_reads_old_routes(
    conn, config, monkeypatch
):
    green_health(conn, config)
    calls, reads = [], []

    def verify(*args, **kwargs):
        calls.append(kwargs)
        return verified_artifact()

    monkeypatch.setattr(readiness, "verify_release", verify)
    conn.set_trace_callback(reads.append)
    report = readiness.readiness_report(conn, config)
    conn.set_trace_callback(None)
    knowledge = report["knowledge_release"]
    assert knowledge["state"] == "artifact_verified_current_runtime_unknown"
    assert knowledge["artifact_verified"] is True
    assert knowledge["current_runtime_verified"] is False
    assert knowledge["descriptor_source"] == "not_observed"
    assert len(calls) == 1 and "runtime_binding" not in calls[0]
    assert not any("route_decisions" in sql for sql in reads)
    assert report["active_ready"] is False


@pytest.mark.parametrize("matches", [False, True])
def test_explicit_actual_descriptor_is_independently_compared(
    conn, config, monkeypatch, matches
):
    calls = []

    def verify(*args, **kwargs):
        calls.append(kwargs)
        return {
            **verified_artifact(),
            "ready": matches if "runtime_binding" in kwargs else False,
            "reason": None if matches else "release_runtime_or_fallback_changed",
        }

    monkeypatch.setattr(readiness, "verify_release", verify)
    descriptor = {"effective_backend": "sqlite", "tokenizer": "fixture"}
    report = readiness.readiness_report(conn, config, actual_runtime_binding=descriptor)
    assert len(calls) == 2 and calls[1]["runtime_binding"] == descriptor
    assert report["knowledge_release"]["artifact_verified"] is True
    assert report["knowledge_release"]["current_runtime_verified"] is matches
    assert report["knowledge_release"]["state"] == (
        "current_runtime_verified" if matches else "current_runtime_blocked"
    )
    assert report["active_ready"] is False  # Not an OS authority assertion.


def test_fixture_evidence_cannot_be_reported_as_production_quality(
    conn, config, monkeypatch
):
    monkeypatch.setattr(
        readiness,
        "verify_release",
        lambda *_args, **_kwargs: {
            **verified_artifact(evidence="synthetic_fixture"),
            "ready": True,
        },
    )
    report = readiness.readiness_report(
        conn, config, actual_runtime_binding={"fixture": True}
    )
    assert report["knowledge_release"]["state"] == "fixture_or_unverified_evidence"
    assert report["knowledge_release"]["human_gold_verified"] is False
    assert report["knowledge_release"]["current_runtime_verified"] is False


def test_report_is_read_only_even_when_auto60_is_expired(conn, config):
    now = datetime.now(UTC)
    conn.execute(
        """INSERT INTO global_control_state
        (scope,mode,revision,outbound_fence,auto_expires_at,changed_by,change_source,changed_at)
        VALUES('feishu_support','auto_60',1,2,?,'owner','fixture',?)""",
        ((now - timedelta(seconds=1)).isoformat(), now.isoformat()),
    )
    initial_changes, original = conn.total_changes, copy.deepcopy(config.raw)
    denied_actions = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_DROP_TABLE,
    }
    attempted_writes = []

    def authorize(action, arg1, arg2, _db, _source):
        if action in denied_actions:
            attempted_writes.append((action, arg1, arg2))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)
    report = readiness.readiness_report(conn, config, now=now)
    conn.set_authorizer(None)
    assert attempted_writes == []
    assert conn.total_changes == initial_changes and config.raw == original
    control = report["authority_boundary"]["global_control"]
    assert control["stored_mode"] == "auto_60"
    assert control["effective_observed_mode"] == "collaborate"
    assert control["materialized"] is False
    assert (
        conn.execute("SELECT mode FROM global_control_state").fetchone()[0] == "auto_60"
    )
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_configuration_failure_is_reported_without_repair_or_crash(conn, config):
    raw = copy.deepcopy(config.raw)
    raw["features"] = []
    broken = Config(raw, config.path)
    report = readiness.readiness_report(conn, broken)
    assert report["authority_boundary"]["config_valid"] is False
    assert report["knowledge_release"]["reason"] == "invalid_configuration"
    assert any(item["code"] == "invalid_configuration" for item in report["blockers"])


def test_broken_heartbeat_is_unknown_not_green_and_no_external_probe(
    conn, config, monkeypatch
):
    heartbeat(conn, "worker", "ready", {})
    conn.execute(
        "UPDATE service_state SET heartbeat_at='invalid' WHERE component='worker'"
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_a, **_k: pytest.fail("readiness must not execute diagnostics"),
    )
    report = readiness.readiness_report(conn, config)
    assert report["runtime_health"]["state"] == "unknown"
    assert report["next_steps"]


def test_timezone_naive_observation_is_rejected(conn, config):
    with pytest.raises(ValueError, match="timezone"):
        readiness.readiness_report(conn, config, now=datetime(2026, 9, 7))  # noqa: DTZ001 - intentional invalid naive observation


def test_approved_inventory_does_not_change_release_readiness(conn, config):
    from test_knowledge_runtime import entry

    green_health(conn, config)
    config.raw["mode"] = "active"
    config.raw["features"]["auto_faq"] = True
    config.raw["identity"]["feishu_owner_open_id"] = "ou_fixture_owner"
    before = readiness.readiness_report(conn, config)
    entry(conn)
    after = readiness.readiness_report(conn, config)
    assert (
        before["runtime_health"]["state"]
        == after["runtime_health"]["state"]
        == "healthy"
    )
    assert (
        before["knowledge_release"]["reason"]
        == after["knowledge_release"]["reason"]
        == "knowledge_release_unconfigured"
    )
    assert after["active_ready"] is False


def test_unknown_side_effect_is_reported_from_actual_permanent_failure_state(
    conn, config
):
    from k3_support.db import transaction
    from k3_support.store import enqueue_outbox

    with transaction(conn):
        key, _ = enqueue_outbox(
            conn,
            channel="telegram",
            action_type="notify",
            destination="fixture-owner",
            payload={"text": "fixture"},
            idempotency_key="fixture-unknown",
        )
    conn.execute(
        "UPDATE outbox SET state='permanent_failure',remote_result_json=? WHERE outbox_id=?",
        ('{"error_type":"uncertain_delivery"}', key),
    )
    report = readiness.readiness_report(conn, config)
    assert "outbox_unknown_outcomes" in {
        item["key"] for item in report["runtime_health"]["alerts"]
    }


def test_release_replacement_between_artifact_and_runtime_checks_is_not_mixed(
    conn, config, monkeypatch
):
    def verify(*args, **kwargs):
        if "runtime_binding" in kwargs:
            return {**verified_artifact(), "ready": True, "release_digest": "b" * 64}
        return verified_artifact()

    monkeypatch.setattr(readiness, "verify_release", verify)
    report = readiness.readiness_report(
        conn, config, actual_runtime_binding={"fixture": True}
    )
    assert report["knowledge_release"]["artifact_verified"] is True
    assert report["knowledge_release"]["current_runtime_verified"] is False
    assert report["knowledge_release"]["reason"] == "release_changed_during_observation"
