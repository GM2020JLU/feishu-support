from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from conftest import config_data

from k3_support.config import ConfigError, validate_config
from k3_support.config_migration import preview_config_migration


@pytest.mark.parametrize("explicit_runtime", [False, True])
def test_legacy_preview_preserves_effective_choices_and_original_file(
    tmp_path, explicit_runtime
):
    raw = config_data(tmp_path, mode="active")
    raw["identity"].update(
        {"feishu_owner_open_id": "ou_original", "feishu_p0_chat_id": "oc_private"}
    )
    raw["scope"] = {
        "technical_chat_ids": ["oc_one", "oc_two"],
        "auto_reply_chat_ids": ["oc_one"],
    }
    raw["notifications"] = {
        "telegram_p0": True,
        "feishu_p0_message": True,
        "feishu_app_urgent": False,
        "feishu_sms_urgent": False,
    }
    if explicit_runtime:
        raw["runtime"] = copy.deepcopy(validate_config(raw)["runtime"])
        raw["runtime"].update(
            {
                "remote_host": "old-builder",
                "remote_workspace_root": "/srv/old-workspace",
                "remote_source_root": "/srv/old-workspace/source",
                "remote_worktree_root": "/srv/old-workspace/worktrees",
            }
        )
    path = tmp_path / "instance.yaml"
    path.write_text(yaml.safe_dump(raw))
    before = path.read_bytes()
    directory_before = set(tmp_path.iterdir())
    report = preview_config_migration(path)
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == directory_before
    proposed = report["proposed_config"]
    assert proposed["schema_version"] == 2
    assert report["effective_choices_preserved"] is True
    assert report["read_only"] is True
    assert validate_config(yaml.safe_load(report["proposed_yaml"])) == proposed
    old_effective = validate_config(raw)
    assert {k: v for k, v in proposed.items() if k != "schema_version"} == {
        k: v for k, v in old_effective.items() if k != "schema_version"
    }
    assert proposed["identity"] == raw["identity"]
    assert proposed["scope"] == raw["scope"]
    assert proposed["policy"]["board_alias"] == "board1"
    assert proposed["notifications"]["feishu_sms_urgent"] is False
    assert proposed["notifications"]["feishu_app_urgent"] is False
    assert proposed["mode"] == "active"
    assert bool(report["legacy_implicit_runtime"]) is not explicit_runtime


def test_legacy_release_context_is_explicit_in_migration(tmp_path):
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.safe_dump(config_data(tmp_path)))
    report = preview_config_migration(
        path,
        legacy_runtime_bin=Path("/opt/original-release/bin"),
        legacy_home=Path("/home/original"),
    )
    proposed = report["proposed_config"]["runtime"]
    assert (
        proposed["codex_remote_command"] == "/opt/original-release/bin/k3-codex-remote"
    )
    assert proposed["board_control_script"].startswith("/home/original/")
    assert report["legacy_resolution_context"]["deployment_verified"] is False
    assert report["runtime_resolution_required"] is True
    assert any(item["field"] == "semantic_command" for item in report["missing_implicit_runtime"])
    assert report["activation_verified"] is False
    assert report["preservation_scope"] == "normalized_config_only_not_installed_legacy_behavior"
    assert proposed["remote_host"] == "buildhost"


def test_new_minimal_instance_preview_does_not_introduce_legacy_defaults(tmp_path):
    raw = config_data(tmp_path)
    raw["schema_version"] = 2
    raw["timezone"] = "Asia/Kathmandu"
    raw["work_hours"] = {"start": "22:00", "end": "02:00"}
    path = tmp_path / "new.yaml"
    path.write_text(yaml.safe_dump(raw))
    report = preview_config_migration(path)
    assert report["requires_migration"] is False
    assert report["legacy_implicit_runtime"] == {}
    assert report["legacy_resolution_context"] is None
    assert report["proposed_config"]["runtime"]["remote_host"] is None
    assert report["proposed_config"]["work_hours"] == raw["work_hours"]


def test_migration_invalid_source_never_modifies_input(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("schema_version: 99\n")
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        preview_config_migration(path)
    assert path.read_bytes() == before
