from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
from conftest import config_data

from k3_support.config import (
    Config,
    ConfigError,
    is_work_time,
    load_config,
    validate_config,
)
from k3_support.coordination import communication_grace_seconds


def test_default_shadow_configuration_is_valid(tmp_path):
    validated = validate_config(config_data(tmp_path))
    assert validated["mode"] == "shadow"
    executable_dir = Path(sys.executable).absolute().parent
    assert validated["runtime"]["codex_remote_command"] == str(
        executable_dir / "k3-codex-remote"
    )
    assert validated["runtime"]["lark_cli_command"] == "lark-cli"
    assert validated["runtime"]["hermes_command"] == "hermes"
    assert validated["notifications"] == {
        "telegram_p0": True,
        "feishu_p0_message": True,
        "feishu_app_urgent": False,
        "feishu_sms_urgent": False,
    }
    assert validated["routing"] == {
        "ai_enabled": True,
        "minimum_route_confidence": 0.8,
        "minimum_clarify_confidence": 0.92,
        "max_clarifications_per_case": 1,
        "profile_ttl_hours": 168,
        "org_profile_lookup": False,
    }


@pytest.mark.parametrize("path,uid,valid", [
    (None, None, True), ("/run/k3-serial/board1.sock", 1234, True),
    (None, 1234, False), ("/run/board1.sock", None, False),
    ("/run/board1.sock", True, False), ("/run/board1.sock", -1, False),
    ("relative.sock", 1234, False), ("/run/../board1.sock", 1234, False),
    ("/run//board1.sock", 1234, False), ("/"+"a"*108, 1234, False),
])
def test_board_observer_endpoint_is_paired_and_bounded(tmp_path, path, uid, valid):
    data = config_data(tmp_path)
    data.setdefault("runtime", {}).update(board_serial_socket=path, board_serial_daemon_uid=uid)
    if valid:
        assert validate_config(data)["runtime"]["board_serial_socket"] == path
    else:
        with pytest.raises(ConfigError, match="board serial"):
            validate_config(data)


def test_mail_share_target_reuses_single_owner_chat_by_default(tmp_path):
    data = config_data(tmp_path)
    data["identity"]["feishu_p0_chat_id"] = "oc_owner_private"
    config = Config(validate_config(data), tmp_path / "config.yaml")
    assert config.mail("summary_share_chat_id") == "oc_owner_private"


@pytest.mark.parametrize("setting", [
    {"backend": "cloud"}, {"candidate_limit": True}, {"candidate_limit": 101},
    {"prefetch_limit": 1}, {"prefetch_limit": 501}, {"allow_fallback": 1},
    {"qdrant_path": "https://example.invalid/index"}, {"qdrant_path": "../index"},
    {"qdrant_collection": "../index"}, {"download_model": True},
])
def test_knowledge_retrieval_config_is_explicit_and_local(tmp_path, setting):
    data = config_data(tmp_path)
    data["knowledge_retrieval"] = setting
    with pytest.raises(ConfigError, match="knowledge_retrieval"):
        validate_config(data)


def test_knowledge_retrieval_defaults_and_optional_index(tmp_path):
    data = config_data(tmp_path)
    assert validate_config(data)["knowledge_retrieval"]["backend"] == "sqlite"
    data["knowledge_retrieval"] = {"backend": "qdrant", "qdrant_path": str(tmp_path / "index")}
    result = validate_config(data)["knowledge_retrieval"]
    assert result["candidate_limit"] == 20 and result["prefetch_limit"] == 80
    assert result["allow_fallback"] is True


def test_notification_channels_are_explicit_and_boolean(tmp_path):
    data = config_data(tmp_path)
    data["notifications"] = {
        "telegram_p0": True,
        "feishu_p0_message": True,
        "feishu_app_urgent": False,
        "feishu_sms_urgent": False,
    }
    assert validate_config(data)["notifications"]["feishu_sms_urgent"] is False
    data["notifications"]["surprise"] = True
    with pytest.raises(ConfigError, match="every known boolean channel"):
        validate_config(data)


def test_routing_policy_is_strict_and_one_question_only(tmp_path):
    data = config_data(tmp_path)
    data["routing"] = {
        "ai_enabled": True,
        "minimum_route_confidence": 0.8,
        "minimum_clarify_confidence": 0.92,
        "max_clarifications_per_case": 1,
        "profile_ttl_hours": 168,
        "org_profile_lookup": False,
    }
    assert validate_config(data)["routing"]["max_clarifications_per_case"] == 1
    data["routing"]["max_clarifications_per_case"] = 2
    with pytest.raises(ConfigError, match="exactly one clarification"):
        validate_config(data)


def test_committed_example_is_a_valid_portable_shadow_config():
    example = Path(__file__).parents[1] / "config" / "config.example.yaml"
    config = load_config(example)
    assert config.mode == "shadow"
    assert config.raw["schema_version"] == 2
    assert config.runtime("remote_host") == "k3-build-host"
    assert "/operator/" not in config.runtime("remote_source_root")
    office_example = example.with_name("chat-mail.example.yaml")
    office = load_config(office_example)
    assert office.raw["schema_version"] == 2
    assert office.feature("mail") is True
    assert office.raw["runtime"]["remote_host"] is None


def test_shadow_refuses_executor_features(tmp_path):
    data = config_data(tmp_path)
    data["features"]["board"] = True
    with pytest.raises(ConfigError, match="shadow mode"):
        validate_config(data)


def test_active_control_feature_requires_stable_telegram_ids(tmp_path):
    data = config_data(tmp_path, mode="active")
    data["features"]["wip_push"] = True
    data["identity"]["telegram_control_user_id"] = None
    with pytest.raises(ConfigError, match="stable Telegram"):
        validate_config(data)


def test_unknown_configuration_key_fails_closed(tmp_path):
    data = config_data(tmp_path)
    data["surprise"] = True
    with pytest.raises(ConfigError, match="unknown top-level"):
        validate_config(data)


def test_repository_mapping_cannot_escape_remote_source_root(tmp_path):
    data = config_data(tmp_path)
    data["repositories"] = {
        "unsafe": {"path": "/tmp/repo", "remote": "origin", "base_branch": "main"}
    }
    with pytest.raises(ConfigError, match="remote_source_root"):
        validate_config(data)


def test_runtime_paths_and_commands_are_portable(tmp_path):
    data = config_data(tmp_path)
    data["runtime"] = {
        "remote_host": "build-user@build-host",
        "remote_workspace_root": "/srv/firmware",
        "remote_source_root": "/srv/firmware/k3",
        "remote_worktree_root": "/srv/firmware/k3-worktrees",
        "ssh_command": "/usr/bin/ssh",
        "serial_command": "/usr/local/bin/serial",
        "lark_cli_command": "/opt/k3/bin/lark-cli",
        "hermes_command": "/opt/k3/bin/hermes",
        "board_control_script": "/opt/k3/board-ctrl.sh",
        "board_boot_script": "/opt/k3/fastboot-boot.sh",
        "codex_remote_command": "/opt/k3/bin/k3-codex-buildhost",
        "codex_board_command": "/opt/k3/bin/k3-codex-board",
    }
    data["repositories"] = {
        "u-boot": {
            "path": "/srv/firmware/k3/u-boot",
            "remote": "origin",
            "base_branch": "main",
        }
    }
    validated = validate_config(data)
    assert validated["runtime"]["remote_host"] == "build-user@build-host"
    assert validated["runtime"]["lark_cli_command"] == "/opt/k3/bin/lark-cli"
    assert validated["repositories"]["u-boot"]["path"] == "/srv/firmware/k3/u-boot"


@pytest.mark.parametrize("roots", [None, "/opt/compiler", ["/"], ["/home/user"],
    ["/run/secrets"], ["/opt/tool/../private"], ["/opt/tool", "/opt/tool/bin"],
    ["/data/home2/operator/WorkSpace/k3"], ["/opt/.private"], ["/usr/share"]])
def test_remote_toolchain_grants_reject_broad_or_ambiguous_paths(tmp_path, roots):
    data = config_data(tmp_path)
    data["runtime"] = {"remote_toolchain_roots": roots}
    with pytest.raises(ConfigError, match="remote_toolchain_roots"):
        validate_config(data)


def test_remote_toolchain_grants_are_explicit_and_need_remote_configuration(tmp_path):
    data = config_data(tmp_path)
    assert validate_config(data)["runtime"]["remote_toolchain_roots"] == []
    data["runtime"] = {"remote_toolchain_roots": ["/opt/riscv-compiler"]}
    assert validate_config(data)["runtime"]["remote_toolchain_roots"] == ["/opt/riscv-compiler"]
    data["schema_version"] = 2
    with pytest.raises(ConfigError, match="configure remote roots"):
        validate_config(data)


def test_runtime_rejects_overlapping_or_relative_roots(tmp_path):
    data = config_data(tmp_path)
    data["runtime"] = {
        "remote_workspace_root": "/srv/firmware",
        "remote_source_root": "/srv/firmware/k3",
        "remote_worktree_root": "/srv/firmware/k3/worktrees",
    }
    with pytest.raises(ConfigError, match="must not contain"):
        validate_config(data)
    data = config_data(tmp_path)
    data["runtime"] = {"remote_source_root": "relative/k3"}
    with pytest.raises(ConfigError, match="absolute safe"):
        validate_config(data)
    data = config_data(tmp_path)
    data["runtime"] = {
        "remote_workspace_root": "/workspace",
        "remote_source_root": "/workspace/source",
        "remote_worktree_root": "/workspace/worktrees",
    }
    with pytest.raises(ConfigError, match="non-root parent"):
        validate_config(data)


def test_new_chat_mail_instance_has_no_personal_execution_targets(tmp_path):
    data = config_data(tmp_path)
    data["schema_version"] = 2
    data["features"]["mail"] = True
    data["policy"]["board_alias"] = "lab-board-4"
    validated = validate_config(data)
    assert all(validated["runtime"][key] is None for key in (
        "remote_host", "remote_workspace_root", "remote_source_root", "remote_worktree_root",
        "board_control_script", "board_boot_script",
    ))
    # Wrapper paths belong to the current installation, which may itself be
    # inside this operator's home. No historical build-host target is inherited.
    assert "/data/home2/operator" not in repr(validated["runtime"])
    assert "buildhost" not in repr(validated["runtime"])
    data["mode"] = "active"
    data["features"]["codex"] = True
    with pytest.raises(ConfigError, match="configure runtime .* explicitly"):
        validate_config(data)


def test_custom_board_cannot_enable_a_mismatched_executor(tmp_path):
    data = config_data(tmp_path, mode="active")
    data["policy"]["board_alias"] = "board2"
    assert validate_config(data)["policy"]["board_alias"] == "board2"
    data["features"]["board"] = True
    with pytest.raises(ConfigError, match="supports only board1"):
        validate_config(data)


@pytest.mark.parametrize(("at", "expected"), [
    ("2026-09-06T16:14:00+00:00", False),
    ("2026-09-06T16:15:00+00:00", True),
    ("2026-09-06T20:14:59+00:00", True),
    ("2026-09-06T20:15:00+00:00", False),
])
def test_overnight_non_hour_offset_schedule_controls_actual_send_grace(tmp_path, at, expected):
    data = config_data(tmp_path)
    data.update({"schema_version": 2, "timezone": "Asia/Kathmandu", "work_hours": {"start": "22:00", "end": "02:00"}})
    cfg = Config(validate_config(data), tmp_path / "config.yaml")
    observed = datetime.fromisoformat(at)
    assert is_work_time(cfg, observed) is expected
    assert communication_grace_seconds(cfg, observed) == (60 if expected else 15)


@pytest.mark.parametrize("hours", [
    {"start": "09:00", "end": "09:00"},
    {"start": "9:00", "end": "18:00"},
    {"start": "24:00", "end": "18:00"},
    {"start": "09:60", "end": "18:00"},
    {"start": "09:00", "end": "18:00", "unexpected": True},
])
def test_schedule_rejects_ambiguous_or_invalid_hours(tmp_path, hours):
    data = config_data(tmp_path)
    data["work_hours"] = hours
    with pytest.raises(ConfigError, match="work_hours"):
        validate_config(data)


def test_named_timezone_validation_and_aware_observation(tmp_path):
    data = config_data(tmp_path)
    data["timezone"] = "Not/AZone"
    with pytest.raises(ConfigError, match="IANA timezone"):
        validate_config(data)
    data["timezone"] = "Europe/London"
    cfg = Config(validate_config(data), tmp_path / "config.yaml")
    with pytest.raises(ConfigError, match="include a timezone"):
        is_work_time(cfg, datetime(2026, 9, 6, 10))  # noqa: DTZ001 - deliberate naive-time rejection test


def test_web_operator_does_not_require_telegram_configuration(tmp_path):
    data = config_data(tmp_path)
    data["identity"].update(control_operator_id="owner:web", telegram_control_user_id=None, telegram_control_chat_id=None)
    validated = validate_config(data)
    assert validated["identity"]["control_operator_id"] == "owner:web"


@pytest.mark.parametrize("operator", [None, "", "with spaces", "a" * 129, 123])
def test_invalid_web_operator_identity_rejected(tmp_path, operator):
    data = config_data(tmp_path)
    data["identity"]["control_operator_id"] = operator
    with pytest.raises(ConfigError, match="control_operator_id"):
        validate_config(data)


@pytest.mark.parametrize("value", [{"channel": "unknown"}, {"channel": []}, {},
                                  {"channel": "web", "destination": "untrusted"}, None])
def test_operator_notifications_reject_invalid_routes(tmp_path, value):
    data = config_data(tmp_path)
    data["operator_notifications"] = value
    with pytest.raises(ConfigError):
        validate_config(data)


def test_feishu_notices_require_the_authenticated_control_identity(tmp_path):
    data = config_data(tmp_path)
    data["operator_notifications"] = {"channel": "feishu"}
    with pytest.raises(ConfigError, match="Feishu"):
        validate_config(data)
    data["identity"].update(control_operator_id="owner", feishu_control_user_id="operator",
                            feishu_control_chat_id="control-chat")
    assert validate_config(data)["operator_notifications"]["channel"] == "feishu"
