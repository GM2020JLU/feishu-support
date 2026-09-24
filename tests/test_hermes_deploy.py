from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from k3_support import hermes_deploy
from k3_support.hermes_deploy import (
    HermesDeployError,
    deployment_plan,
    doctor,
    install_plugin,
    rollback_plugin,
)


def executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path.resolve()


def test_plan_is_read_only_and_requires_absolute_valid_inputs(tmp_path):
    home = tmp_path / "hermes"
    cli = executable(tmp_path / "k3-supportctl")
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")
    plan = deployment_plan(
        hermes_home=home.resolve(),
        control_cli=cli,
        control_config=control.resolve(),
    )
    assert plan["plugin"] == "k3-support-control"
    assert plan["skill"] == "k3-support-orchestrator"
    assert plan["skill_target"].endswith(
        "/skills/software-development/k3-support-orchestrator"
    )
    assert plan["requires_gateway_restart"] is True
    assert not home.exists()
    with pytest.raises(HermesDeployError, match="absolute"):
        deployment_plan(
            hermes_home="relative",
            control_cli=cli,
            control_config=control.resolve(),
        )


def test_plan_pins_symlinked_control_cli_and_packages_all_skill_references(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    cli = executable(release / "k3-supportctl")
    current = tmp_path / "current"
    current.symlink_to(release, target_is_directory=True)
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")

    plan = deployment_plan(
        hermes_home=(tmp_path / "hermes").resolve(),
        control_cli=current / "k3-supportctl",
        control_config=control.resolve(),
    )

    assert plan["runtime"]["control_cli"] == str(cli)
    assert "references/audience-routing.md" in plan["skill_source_hashes"]


def test_install_doctor_and_rollback_preserve_existing_config(tmp_path):
    home = (tmp_path / "hermes").resolve()
    home.mkdir()
    original_config = {
        "telegram": {"token": "secret-looking-value"},
        "plugins": {
            "enabled": ["existing"],
            "disabled": ["k3-support-control", "other-disabled"],
        },
    }
    config_path = home / "config.yaml"
    original_bytes = yaml.safe_dump(original_config, sort_keys=False).encode()
    config_path.write_bytes(original_bytes)
    cli = executable(tmp_path / "k3-supportctl")
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")

    result = install_plugin(
        hermes_home=home,
        control_cli=cli,
        control_config=control.resolve(),
    )
    assert result["installed"] is True
    assert result["doctor"]["ready_for_restart"] is True
    installed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert installed["telegram"]["token"] == "secret-looking-value"
    assert installed["plugins"]["enabled"] == ["existing", "k3-support-control"]
    assert installed["plugins"]["disabled"] == ["other-disabled"]
    runtime_path = home / "k3-support/control-plugin.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert runtime["control_cli"] == str(cli)
    assert stat_mode(runtime_path) == 0o600
    assert (home / "plugins/k3-support-control/plugin.yaml").is_file()
    assert (home / "plugins/k3-support-control/feishu_cards.py").is_file()
    skill = home / "skills/software-development/k3-support-orchestrator/SKILL.md"
    assert skill.is_file()
    assert (
        home
        / "skills/software-development/k3-support-orchestrator/references/audience-routing.md"
    ).is_file()
    assert "/home/operator" not in skill.read_text(encoding="utf-8")

    rollback_plugin(backup_root=result["backup"])
    assert config_path.read_bytes() == original_bytes
    assert not (home / "plugins/k3-support-control").exists()
    assert not (home / "skills/software-development/k3-support-orchestrator").exists()
    assert not runtime_path.exists()


def test_install_upgrade_can_rollback_previous_plugin(tmp_path):
    home = (tmp_path / "hermes").resolve()
    old_plugin = home / "plugins/k3-support-control"
    old_plugin.mkdir(parents=True)
    (old_plugin / "old.txt").write_text("old", encoding="utf-8")
    old_skill = home / "skills/software-development/k3-support-orchestrator"
    old_skill.mkdir(parents=True)
    (old_skill / "old.txt").write_text("old skill", encoding="utf-8")
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    cli = executable(tmp_path / "k3-supportctl")
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")

    result = install_plugin(
        hermes_home=home,
        control_cli=cli,
        control_config=control.resolve(),
    )
    assert not (old_plugin / "old.txt").exists()
    rollback_plugin(backup_root=result["backup"])
    assert (old_plugin / "old.txt").read_text(encoding="utf-8") == "old"
    assert (old_skill / "old.txt").read_text(encoding="utf-8") == "old skill"


def test_mid_install_failure_restores_plugin_skill_runtime_and_config(
    tmp_path, monkeypatch
):
    home = (tmp_path / "hermes").resolve()
    plugin = home / "plugins/k3-support-control"
    plugin.mkdir(parents=True)
    (plugin / "old.txt").write_text("old plugin")
    skill = home / "skills/software-development/k3-support-orchestrator"
    skill.mkdir(parents=True)
    (skill / "old.txt").write_text("old skill")
    runtime = home / "k3-support/control-plugin.json"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("old runtime")
    config_path = home / "config.yaml"
    config_path.write_text("plugins:\n  enabled: []\n")
    original_config = config_path.read_bytes()
    cli = executable(tmp_path / "k3-supportctl")
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")
    original_atomic_write = hermes_deploy._atomic_write

    def fail_runtime_write(path, data, *, mode=0o600):
        if path == runtime:
            raise OSError("injected runtime write failure")
        return original_atomic_write(path, data, mode=mode)

    monkeypatch.setattr(hermes_deploy, "_atomic_write", fail_runtime_write)
    with pytest.raises(HermesDeployError, match="rolled back"):
        install_plugin(
            hermes_home=home,
            control_cli=cli,
            control_config=control.resolve(),
        )
    assert (plugin / "old.txt").read_text() == "old plugin"
    assert (skill / "old.txt").read_text() == "old skill"
    assert runtime.read_text() == "old runtime"
    assert config_path.read_bytes() == original_config


def test_install_refuses_symlink_targets(tmp_path):
    home = (tmp_path / "hermes").resolve()
    home.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (home / "plugins").mkdir()
    (home / "plugins/k3-support-control").symlink_to(target, target_is_directory=True)
    cli = executable(tmp_path / "k3-supportctl")
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")
    with pytest.raises(HermesDeployError, match="symlinked"):
        install_plugin(
            hermes_home=home,
            control_cli=cli,
            control_config=control.resolve(),
        )
    assert target.exists()


def test_install_refuses_non_directory_plugin_target(tmp_path):
    home = (tmp_path / "hermes").resolve()
    (home / "plugins").mkdir(parents=True)
    (home / "plugins/k3-support-control").write_text("not a directory")
    cli = executable(tmp_path / "k3-supportctl")
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")
    with pytest.raises(HermesDeployError, match="not a directory"):
        install_plugin(
            hermes_home=home,
            control_cli=cli,
            control_config=control.resolve(),
        )


def test_install_refuses_symlinked_skill_target(tmp_path):
    home = (tmp_path / "hermes").resolve()
    target = tmp_path / "outside-skill"
    target.mkdir()
    skill_parent = home / "skills/software-development"
    skill_parent.mkdir(parents=True)
    (skill_parent / "k3-support-orchestrator").symlink_to(
        target, target_is_directory=True
    )
    cli = executable(tmp_path / "k3-supportctl")
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")
    with pytest.raises(HermesDeployError, match="symlinked"):
        install_plugin(
            hermes_home=home,
            control_cli=cli,
            control_config=control.resolve(),
        )
    assert target.is_dir()


def test_rollback_rejects_tampered_targets(tmp_path):
    home = (tmp_path / "hermes").resolve()
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n")
    cli = executable(tmp_path / "k3-supportctl")
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")
    result = install_plugin(
        hermes_home=home,
        control_cli=cli,
        control_config=control.resolve(),
    )
    manifest_path = Path(result["backup"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    victim = tmp_path / "victim"
    victim.mkdir()
    manifest["target"] = str(victim)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(HermesDeployError, match="unexpected targets"):
        rollback_plugin(backup_root=result["backup"])
    assert victim.is_dir()


def test_rollback_accepts_legacy_plugin_only_manifest_without_touching_skill(tmp_path):
    home = (tmp_path / "hermes").resolve()
    target = home / "plugins/k3-support-control"
    target.mkdir(parents=True)
    (target / "new.txt").write_text("new")
    skill = home / "skills/software-development/k3-support-orchestrator"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("current skill")
    runtime = home / "k3-support/control-plugin.json"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("new runtime")
    config = home / "config.yaml"
    config.write_text("new config")
    backup = home / "k3-support/deploy-backups/legacy"
    (backup / "plugin").mkdir(parents=True)
    (backup / "plugin/old.txt").write_text("old")
    (backup / "control-plugin.json").write_text("old runtime")
    (backup / "config.yaml").write_text("old config")
    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "hermes_home": str(home),
                "plugin_existed": True,
                "runtime_existed": True,
                "config_existed": True,
                "target": str(target),
                "runtime_config": str(runtime),
                "hermes_config": str(config),
            }
        )
    )
    rollback_plugin(backup_root=backup)
    assert (target / "old.txt").read_text() == "old"
    assert (skill / "SKILL.md").read_text() == "current skill"
    assert runtime.read_text() == "old runtime"
    assert config.read_text() == "old config"


def test_doctor_reports_incomplete_install(tmp_path):
    status = doctor(hermes_home=(tmp_path / "hermes").resolve())
    assert status["ready_for_restart"] is False
    assert status["problems"]


def test_doctor_rejects_tampered_plugin_file(tmp_path):
    home = (tmp_path / "hermes").resolve()
    home.mkdir()
    cli = executable(tmp_path / "k3-supportctl")
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")
    install_plugin(
        hermes_home=home,
        control_cli=cli,
        control_config=control.resolve(),
    )
    plugin = home / "plugins/k3-support-control/__init__.py"
    plugin.write_text(plugin.read_text(encoding="utf-8") + "\n# tampered\n")
    status = doctor(hermes_home=home)
    assert status["ready_for_restart"] is False
    assert "plugin file differs from package: __init__.py" in status["problems"]


def test_doctor_rejects_tampered_skill_file(tmp_path):
    home = (tmp_path / "hermes").resolve()
    home.mkdir()
    cli = executable(tmp_path / "k3-supportctl")
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")
    install_plugin(
        hermes_home=home,
        control_cli=cli,
        control_config=control.resolve(),
    )
    skill = home / "skills/software-development/k3-support-orchestrator/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nchanged\n")
    status = doctor(hermes_home=home)
    assert status["ready_for_restart"] is False
    assert "skill file differs from package: SKILL.md" in status["problems"]


def test_packaged_plugin_loads_in_installed_hermes(tmp_path):
    hermes_python = (
        Path.home() / ".hermes/hermes-agent/venv/bin/python"
    )
    if not hermes_python.is_file():
        pytest.skip("Hermes runtime is not installed")
    home = (tmp_path / "hermes").resolve()
    home.mkdir()
    cli = executable(tmp_path / "k3-supportctl")
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")
    install_plugin(
        hermes_home=home,
        control_cli=cli,
        control_config=control.resolve(),
    )
    program = (
        "from hermes_cli.plugins import discover_plugins,get_plugin_manager;"
        "from agent.skill_utils import get_all_skills_dirs,iter_skill_index_files,parse_frontmatter;"
        "discover_plugins(force=True);"
        "m=get_plugin_manager();"
        "assert 'k3-support-control' in m._plugins;"
        "p=m._plugins['k3-support-control'];"
        "assert p.enabled and p.hooks_registered==['pre_gateway_dispatch'];"
        "assert 'feishu' in m._plugin_commands;"
        "found=[str(path) for root in get_all_skills_dirs() if root.exists() "
        "for path in iter_skill_index_files(root,'SKILL.md') "
        "if parse_frontmatter(path.read_text(encoding='utf-8'))[0].get('name')"
        "=='k3-support-orchestrator'];"
        "assert len(found)==1,found"
    )
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    result = subprocess.run(
        [str(hermes_python), "-c", program],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
