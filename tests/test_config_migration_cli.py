from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from conftest import config_data

from k3_support.cli import main


def test_config_preview_cli_is_read_only_and_does_not_initialize_database(tmp_path, capsys):
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.safe_dump(config_data(tmp_path)))
    before = path.read_bytes()
    assert main(["--config", str(path), "config-migration-preview"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["read_only"] is True
    assert report["effective_choices_preserved"] is True
    assert path.read_bytes() == before
    assert not Path(report["proposed_config"]["paths"]["database"]).exists()
    assert set(tmp_path.iterdir()) == {path}


def test_config_preview_cli_exports_only_a_new_private_copy(tmp_path, capsys):
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.safe_dump(config_data(tmp_path)))
    before = path.read_bytes()
    target = tmp_path / "explicit.yaml"
    argv = [
        "--config", str(path), "config-migration-preview", "--output", str(target),
        "--legacy-runtime-bin", "/opt/original/bin", "--legacy-home", "/home/operator",
    ]
    assert main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert target.stat().st_mode & 0o777 == 0o600
    assert yaml.safe_load(target.read_text())["runtime"]["codex_remote_command"] == "/opt/original/bin/k3-codex-remote"
    assert report["legacy_resolution_context"]["deployment_verified"] is False
    assert main(argv) == 2
    assert "FileExistsError" in capsys.readouterr().err
    assert path.read_bytes() == before


@pytest.mark.parametrize("same_source", [False, True])
def test_config_export_refuses_existing_or_symlink_target(tmp_path, capsys, same_source):
    path = tmp_path / "source.yaml"
    path.write_text(yaml.safe_dump(config_data(tmp_path)))
    before = path.read_bytes()
    target = path if same_source else tmp_path / "link.yaml"
    if not same_source:
        target.symlink_to(path)
    assert main(["--config", str(path), "config-migration-preview", "--output", str(target)]) == 2
    assert "FileExistsError" in capsys.readouterr().err
    assert path.read_bytes() == before
