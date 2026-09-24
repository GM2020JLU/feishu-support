"""Read-only, explicit conversion of an instance configuration to schema v2."""

from __future__ import annotations

import copy
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from .config import ConfigError, _runtime_defaults, validate_config
from .ids import digest


def _absolute_context(value: Path | None, default: Path, label: str) -> Path:
    path = value.expanduser() if value is not None else default
    if not path.is_absolute():
        raise ConfigError(f"{label} must be an absolute path")
    return path


def preview_config_migration(
    path: str | Path,
    *,
    legacy_runtime_bin: Path | None = None,
    legacy_home: Path | None = None,
) -> dict[str, Any]:
    """Return a validated explicit copy; never write, connect, migrate or activate.

    For v1, omitted executable/script paths were relative to the interpreting
    release and user's home. Supply those old locations when previewing an
    upgrade from a different release. The report always identifies the assumed
    context; it does not claim to have inspected a live service installation.
    """
    source = Path(path).expanduser().absolute()
    payload = source.read_bytes()
    raw = yaml.safe_load(payload)
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a mapping")
    version = raw.get("schema_version")
    context_bin = _absolute_context(
        legacy_runtime_bin, Path(sys.executable).absolute().parent, "legacy_runtime_bin"
    )
    context_home = _absolute_context(legacy_home, Path.home(), "legacy_home")
    interpreted = copy.deepcopy(raw)
    supplied_runtime = raw.get("runtime", {})
    if not isinstance(supplied_runtime, dict):
        raise ConfigError("runtime must be a mapping")
    implicit: dict[str, Any] = {}
    if version == 1:
        defaults = _runtime_defaults(schema_version=1)
        defaults.update(
            {
                "codex_remote_command": str(context_bin / "k3-codex-remote"),
                "semantic_command": str(context_bin / "k3-support-hermes-stdin"),
                "codex_board_command": str(context_bin / "k3-codex-board"),
                "board_control_script": str(
                    context_home / ".codex/skills/board-ctrl/scripts/board-ctrl.sh"
                ),
                "board_boot_script": str(
                    context_home / ".codex/skills/board-ctrl/scripts/fastboot-boot.sh"
                ),
            }
        )
        implicit = {
            key: value for key, value in defaults.items() if key not in supplied_runtime
        }
        interpreted["runtime"] = {**defaults, **supplied_runtime}
    old_effective = validate_config(interpreted)
    proposed = copy.deepcopy(old_effective)
    proposed["schema_version"] = 2
    proposed = validate_config(proposed)
    before = {
        key: value for key, value in old_effective.items() if key != "schema_version"
    }
    after = {key: value for key, value in proposed.items() if key != "schema_version"}
    if before != after:
        raise ConfigError("migration would change effective instance choices")
    missing_runtime = [
        {"field": key, "path": value, "reason": "missing_or_not_executable"}
        for key, value in implicit.items()
        if key.endswith(("_command", "_script"))
        and isinstance(value, str)
        and (not Path(value).is_file() or not os.access(value, os.X_OK))
    ]
    return {
        "source_path": str(source),
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "from_schema_version": version,
        "to_schema_version": 2,
        "read_only": True,
        "effective_choices_preserved": True,
        "preservation_scope": "normalized_config_only_not_installed_legacy_behavior",
        "missing_implicit_runtime": missing_runtime,
        "runtime_resolution_required": bool(missing_runtime),
        "activation_verified": False,
        "requires_migration": version == 1,
        "legacy_implicit_runtime": implicit,
        "legacy_resolution_context": {
            "runtime_bin": str(context_bin),
            "home": str(context_home),
            "deployment_verified": False,
        }
        if version == 1
        else None,
        "old_effective_config": old_effective,
        "proposed_config": proposed,
        "proposed_config_digest": digest(proposed),
        "proposed_yaml": yaml.safe_dump(proposed, allow_unicode=True, sort_keys=False),
    }
