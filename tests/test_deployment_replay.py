from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from k3_support.config import Config
from k3_support.deployment_replay import (
    DeploymentReplayError,
    capture_snapshot,
    compare_snapshot,
    load_snapshot,
    write_snapshot,
)
from k3_support.systemd_deploy import SERVICE_ENTRYPOINTS


def _runtime(tmp_path: Path, base: Config) -> tuple[Config, Path, Path, Path]:
    data = deepcopy(base.raw)
    data["identity"]["feishu_owner_open_id"] = "ou_secret_owner"
    data["mail"]["summary_share_chat_id"] = "oc_secret_mail_chat"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    config = Config(data, config_path)
    release = tmp_path / "release" / "bin"
    release.mkdir(parents=True)
    for name in ["k3-supportctl", *SERVICE_ENTRYPOINTS.values()]:
        path = release / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    hermes_home = tmp_path / ".hermes"
    return config, release / "k3-supportctl", unit_dir, hermes_home


def test_deployment_snapshot_is_replayable_and_excludes_identity_values(
    tmp_path, config
):
    config, cli, unit_dir, hermes_home = _runtime(tmp_path, config)
    snapshot = capture_snapshot(
        config,
        control_cli=cli,
        unit_dir=unit_dir,
        hermes_home=hermes_home,
    )
    target = write_snapshot(snapshot, tmp_path / "deployment.json")
    loaded = load_snapshot(target)
    current = capture_snapshot(
        config,
        control_cli=cli,
        unit_dir=unit_dir,
        hermes_home=hermes_home,
    )
    result = compare_snapshot(loaded, current)

    assert result["matches"] is True
    serialized = target.read_text(encoding="utf-8")
    assert "ou_secret_owner" not in serialized
    assert "oc_secret_mail_chat" not in serialized
    assert loaded["policy"]["identity_presence"]["feishu_owner_open_id"] is True
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_deployment_replay_reports_policy_drift_without_applying(tmp_path, config):
    config, cli, unit_dir, hermes_home = _runtime(tmp_path, config)
    expected = capture_snapshot(
        config,
        control_cli=cli,
        unit_dir=unit_dir,
        hermes_home=hermes_home,
    )
    config.raw["features"]["auto_faq"] = True
    current = capture_snapshot(
        config,
        control_cli=cli,
        unit_dir=unit_dir,
        hermes_home=hermes_home,
    )

    result = compare_snapshot(expected, current)
    assert result["matches"] is False
    assert "policy" in result["changed_sections"]
    assert result["apply_performed"] is False


def test_deployment_snapshot_rejects_tampering(tmp_path, config):
    config, cli, unit_dir, hermes_home = _runtime(tmp_path, config)
    snapshot = capture_snapshot(
        config,
        control_cli=cli,
        unit_dir=unit_dir,
        hermes_home=hermes_home,
    )
    target = write_snapshot(snapshot, tmp_path / "deployment.json")
    value = json.loads(target.read_text(encoding="utf-8"))
    value["policy"]["mode"] = "active"
    target.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(DeploymentReplayError, match="digest"):
        load_snapshot(target)


def test_runtime_feature_drift_is_captured_without_mutation(tmp_path, config, conn):
    from k3_support import feature_settings

    config, cli, units, hermes = _runtime(tmp_path, config)
    args = {"control_cli": cli, "unit_dir": units, "hermes_home": hermes}
    initial = capture_snapshot(config, **args)
    draft = feature_settings.preview(
        conn,
        config,
        session_id="test-session",
        values={**config.raw["features"], "codex": True},
        expected_revision=0,
    )
    feature_settings.apply(
        conn,
        config,
        session_id="test-session",
        actor_id="fixture-private-feature-actor",
        draft_id=draft["draft_id"],
    )
    before = conn.serialize()
    updated = capture_snapshot(config, **args)
    assert updated["policy"]["features"]["codex"] is True
    assert updated["policy"]["base_features"]["codex"] is False
    assert updated["policy"]["feature_settings_revision"] == 1
    assert updated["policy"]["feature_settings_state"] == "override"
    assert compare_snapshot(initial, updated)["changed_sections"] == ["policy"]
    assert conn.serialize() == before
    serialized = json.dumps(updated)
    assert "fixture-private-feature-actor" not in serialized
    assert "test-session" not in serialized


def test_unverifiable_overrides_cannot_be_captured_as_all_off(tmp_path, config, conn):
    from k3_support import feature_settings

    config, cli, units, hermes = _runtime(tmp_path, config)
    draft = feature_settings.preview(
        conn,
        config,
        session_id="test-session",
        values={**config.raw["features"], "codex": True},
        expected_revision=0,
    )
    feature_settings.apply(
        conn,
        config,
        session_id="test-session",
        actor_id="owner",
        draft_id=draft["draft_id"],
    )
    config.raw["work_hours"]["start"] = "10:00"
    before = conn.serialize()
    with pytest.raises(DeploymentReplayError, match="cannot be verified"):
        capture_snapshot(config, control_cli=cli, unit_dir=units, hermes_home=hermes)
    assert conn.serialize() == before
