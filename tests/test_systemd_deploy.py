from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from k3_support.systemd_deploy import (
    SERVICE_ENTRYPOINTS,
    UNIT_NAMES,
    SystemdDeployError,
    deployment_plan,
    doctor,
    install_units,
    render_units,
    rollback_units,
)


def runtime(tmp_path: Path, config) -> tuple[Path, Path]:
    config.path.write_text(
        yaml.safe_dump(config.raw, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "venv/bin"
    bin_dir.mkdir(parents=True)
    for name in {"k3-supportctl", *SERVICE_ENTRYPOINTS.values()}:
        executable = bin_dir / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
    return (bin_dir / "k3-supportctl").resolve(), config.path.resolve()


def test_systemd_plan_is_read_only_and_portable(tmp_path, config):
    cli, control = runtime(tmp_path, config)
    unit_dir = (tmp_path / ".config/systemd/user").resolve()
    plan = deployment_plan(
        unit_dir=unit_dir,
        control_cli=cli,
        control_config=control,
        service_path="/opt/lark/bin:/usr/bin",
    )
    assert plan["unit_count"] == len(UNIT_NAMES)
    assert plan["services_started"] is False
    assert not unit_dir.exists()
    units = render_units(
        control_cli=cli,
        control_config=control,
        service_path="/opt/lark/bin:/usr/bin",
    )
    assert set(units) == set(UNIT_NAMES)
    retention = units['k3-support-body-retention.service']
    assert 'Type=simple' in retention and 'Restart=on-failure' in retention
    assert 'k3-support-body-retention' in retention and '--config' in retention
    assert '--once' not in retention
    combined = "\n".join(units.values())
    assert "/home/operator/Workspace/k3-support" not in combined
    assert str(cli.parent) in combined
    assert str(control) in combined
    assert "12:00:00 Asia/Shanghai" in units["k3-support-mail-noon.timer"]
    assert "18:00:00 Asia/Shanghai" in units["k3-support-mail-evening.timer"]


def test_render_units_pins_symlinked_cli_to_exact_release(tmp_path, config):
    cli, control = runtime(tmp_path / "release", config)
    current = tmp_path / "current"
    current.symlink_to(cli.parent.parent, target_is_directory=True)

    units = render_units(
        control_cli=current / "bin/k3-supportctl",
        control_config=control,
    )

    assert str(cli) in units["k3-support-backup.service"]
    assert str(current) not in "\n".join(units.values())


def test_systemd_install_doctor_tamper_and_rollback(tmp_path, config):
    cli, control = runtime(tmp_path, config)
    unit_dir = (tmp_path / ".config/systemd/user").resolve()
    unit_dir.mkdir(parents=True)
    old = unit_dir / "k3-support-worker.service"
    old.write_bytes(b"old exact bytes\n")
    result = install_units(
        unit_dir=unit_dir,
        control_cli=cli,
        control_config=control,
        service_path="/usr/bin:/bin",
    )
    assert result["installed"] is True
    assert result["services_started"] is False
    assert result["doctor"]["ready_for_reload"] is True
    assert all((unit_dir / name).is_file() for name in UNIT_NAMES)

    target = unit_dir / "k3-support-outbox.service"
    target.write_text(target.read_text() + "# tampered\n")
    status = doctor(
        unit_dir=unit_dir,
        control_cli=cli,
        control_config=control,
        service_path="/usr/bin:/bin",
    )
    assert status["ready_for_reload"] is False
    assert status["unit_contract_ready"] is False
    assert status["runtime_state_checked"] is False
    assert status["daemon_reload_required"] is None
    assert status["services_started"] is None
    assert "unit differs from rendered contract: k3-support-outbox.service" in status[
        "problems"
    ]

    rollback_units(backup_root=result["backup"])
    assert old.read_bytes() == b"old exact bytes\n"
    assert not (unit_dir / "k3-support-ingress.service").exists()


def test_systemd_install_refuses_symlinked_unit(tmp_path, config):
    cli, control = runtime(tmp_path, config)
    unit_dir = (tmp_path / ".config/systemd/user").resolve()
    unit_dir.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (unit_dir / "k3-support-worker.service").symlink_to(victim)
    with pytest.raises(SystemdDeployError, match="unsafe unit"):
        install_units(
            unit_dir=unit_dir,
            control_cli=cli,
            control_config=control,
        )
    assert victim.read_text() == "keep"


def test_systemd_rollback_rejects_tampered_unit_set(tmp_path, config):
    cli, control = runtime(tmp_path, config)
    unit_dir = (tmp_path / ".config/systemd/user").resolve()
    result = install_units(
        unit_dir=unit_dir,
        control_cli=cli,
        control_config=control,
    )
    manifest_path = Path(result["backup"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["units"].append("unrelated.service")
    manifest_path.write_text(json.dumps(manifest))
    unrelated = unit_dir / "unrelated.service"
    unrelated.write_text("keep")
    with pytest.raises(SystemdDeployError, match="invalid unit set"):
        rollback_units(backup_root=result["backup"])
    assert unrelated.read_text() == "keep"


def test_rendered_units_pass_systemd_analyze_verify(tmp_path, config):
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze is not installed")
    cli, control = runtime(tmp_path, config)
    unit_dir = (tmp_path / ".config/systemd/user").resolve()
    install_units(
        unit_dir=unit_dir,
        control_cli=cli,
        control_config=control,
        service_path="/usr/bin:/bin",
    )
    result = subprocess.run(
        [analyzer, "verify", *[str(unit_dir / name) for name in UNIT_NAMES]],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
