from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

PLUGIN_NAME = "k3-support-control"
SKILL_NAME = "k3-support-orchestrator"
RUNTIME_SCHEMA_VERSION = 1
PLUGIN_FILES = ("__init__.py", "feishu_cards.py", "plugin.yaml", "README.md")
SKILL_FILES = (
    "SKILL.md",
    "references/operator-policy.md",
    "references/evidence-review.md",
    "references/office-workflows.md",
    "references/audience-routing.md",
)


class HermesDeployError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentPaths:
    hermes_home: Path
    target: Path
    skill_target: Path
    runtime_config: Path
    hermes_config: Path
    backup_root: Path


def _absolute(path: str | Path, label: str) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        raise HermesDeployError(f"{label} must be an absolute path")
    return value


def deployment_paths(hermes_home: str | Path) -> DeploymentPaths:
    home = _absolute(hermes_home, "hermes_home")
    if home == Path(home.anchor):
        raise HermesDeployError("hermes_home cannot be a filesystem root")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return DeploymentPaths(
        hermes_home=home,
        target=home / "plugins" / PLUGIN_NAME,
        skill_target=home / "skills" / "software-development" / SKILL_NAME,
        runtime_config=home / "k3-support" / "control-plugin.json",
        hermes_config=home / "config.yaml",
        backup_root=home / "k3-support" / "deploy-backups" / timestamp,
    )


def _plugin_source() -> Any:
    return files("k3_support.hermes_plugin")


def _skill_source() -> Any:
    return files("k3_support.hermes_skill")


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _runtime_value(
    *, control_cli: str | Path, control_config: str | Path, timeout_seconds: int
) -> dict[str, Any]:
    # Pin versioned targets even when deployment is invoked through a
    # convenient ``current`` symlink. Hermes must not silently change code on
    # the next symlink rotation without another reviewed install/restart.
    cli = _absolute(control_cli, "control_cli").resolve()
    config = _absolute(control_config, "control_config").resolve()
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise HermesDeployError("control_cli does not exist or is not executable")
    if not config.is_file():
        raise HermesDeployError("control_config does not exist")
    if not 1 <= timeout_seconds <= 30:
        raise HermesDeployError("timeout_seconds must be between 1 and 30")
    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "control_cli": str(cli),
        "control_config": str(config),
        "timeout_seconds": timeout_seconds,
    }


def _load_hermes_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise HermesDeployError("Hermes config is not valid YAML") from exc
    if not isinstance(value, dict):
        raise HermesDeployError("Hermes config root must be a mapping")
    return value


def _enable_plugin(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    plugins = result.get("plugins")
    if plugins is None:
        plugins = {}
    if not isinstance(plugins, dict):
        raise HermesDeployError("Hermes plugins config must be a mapping")
    plugins = dict(plugins)
    enabled = plugins.get("enabled", [])
    disabled = plugins.get("disabled", [])
    if not isinstance(enabled, list) or not all(isinstance(x, str) for x in enabled):
        raise HermesDeployError("Hermes plugins.enabled must be a string list")
    if not isinstance(disabled, list) or not all(
        isinstance(x, str) for x in disabled
    ):
        raise HermesDeployError("Hermes plugins.disabled must be a string list")
    plugins["enabled"] = list(dict.fromkeys([*enabled, PLUGIN_NAME]))
    plugins["disabled"] = [item for item in disabled if item != PLUGIN_NAME]
    result["plugins"] = plugins
    return result


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_plugin_to_staging(paths: DeploymentPaths) -> Path:
    paths.target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{PLUGIN_NAME}.", dir=paths.target.parent)
    )
    source = _plugin_source()
    try:
        for name in PLUGIN_FILES:
            item = source.joinpath(name)
            if not item.is_file():
                raise HermesDeployError(f"packaged plugin is missing {name}")
            destination = staging / name
            destination.write_bytes(item.read_bytes())
            os.chmod(destination, 0o644)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _copy_skill_to_staging(paths: DeploymentPaths) -> Path:
    paths.skill_target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{SKILL_NAME}.", dir=paths.skill_target.parent)
    )
    source = _skill_source()
    try:
        for name in SKILL_FILES:
            item = source.joinpath(name)
            if not item.is_file():
                raise HermesDeployError(f"packaged skill is missing {name}")
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(item.read_bytes())
            os.chmod(destination, 0o644)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def deployment_plan(
    *,
    hermes_home: str | Path,
    control_cli: str | Path,
    control_config: str | Path,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    paths = deployment_paths(hermes_home)
    runtime = _runtime_value(
        control_cli=control_cli,
        control_config=control_config,
        timeout_seconds=timeout_seconds,
    )
    hermes_config = _load_hermes_config(paths.hermes_config)
    enabled_config = _enable_plugin(hermes_config)
    before_plugins = hermes_config.get("plugins") or {}
    after_plugins = enabled_config.get("plugins") or {}
    before_enabled = before_plugins.get("enabled") or []
    before_disabled = before_plugins.get("disabled") or []
    after_enabled = after_plugins.get("enabled") or []
    after_disabled = after_plugins.get("disabled") or []
    source = _plugin_source()
    skill_source = _skill_source()
    return {
        "plugin": PLUGIN_NAME,
        "skill": SKILL_NAME,
        "hermes_home": str(paths.hermes_home),
        "target": str(paths.target),
        "skill_target": str(paths.skill_target),
        "runtime_config": str(paths.runtime_config),
        "hermes_config": str(paths.hermes_config),
        "would_replace_plugin": paths.target.exists(),
        "would_replace_skill": paths.skill_target.exists(),
        "would_update_hermes_config": True,
        "config_changes": {
            "enable_plugin": PLUGIN_NAME not in before_enabled
            and PLUGIN_NAME in after_enabled,
            "remove_explicit_disable": PLUGIN_NAME in before_disabled
            and PLUGIN_NAME not in after_disabled,
        },
        "requires_gateway_restart": True,
        "runtime": runtime,
        "source_hashes": {
            name: hashlib.sha256(source.joinpath(name).read_bytes()).hexdigest()
            for name in PLUGIN_FILES
        },
        "skill_source_hashes": {
            name: hashlib.sha256(skill_source.joinpath(name).read_bytes()).hexdigest()
            for name in SKILL_FILES
        },
    }


def install_plugin(
    *,
    hermes_home: str | Path,
    control_cli: str | Path,
    control_config: str | Path,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    paths = deployment_paths(hermes_home)
    for protected_path in (
        paths.target,
        paths.skill_target,
        paths.runtime_config,
        paths.hermes_config,
    ):
        if protected_path.is_symlink():
            raise HermesDeployError(
                f"refusing to replace symlinked path: {protected_path}"
            )
    if paths.target.exists() and not paths.target.is_dir():
        raise HermesDeployError("plugin target exists and is not a directory")
    if paths.skill_target.exists() and not paths.skill_target.is_dir():
        raise HermesDeployError("skill target exists and is not a directory")
    runtime = _runtime_value(
        control_cli=control_cli,
        control_config=control_config,
        timeout_seconds=timeout_seconds,
    )
    hermes_config = _load_hermes_config(paths.hermes_config)
    enabled_config = _enable_plugin(hermes_config)
    staging: Path | None = None
    skill_staging: Path | None = None
    try:
        staging = _copy_plugin_to_staging(paths)
        skill_staging = _copy_skill_to_staging(paths)
    except Exception:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    paths.backup_root.mkdir(parents=True, mode=0o700)
    backup_plugin = paths.backup_root / "plugin"
    backup_skill = paths.backup_root / "skill"
    backup_runtime = paths.backup_root / "control-plugin.json"
    backup_config = paths.backup_root / "config.yaml"
    manifest = {
        "schema_version": 2,
        "hermes_home": str(paths.hermes_home),
        "plugin_existed": paths.target.exists(),
        "skill_existed": paths.skill_target.exists(),
        "runtime_existed": paths.runtime_config.exists(),
        "config_existed": paths.hermes_config.exists(),
        "target": str(paths.target),
        "skill_target": str(paths.skill_target),
        "runtime_config": str(paths.runtime_config),
        "hermes_config": str(paths.hermes_config),
    }
    if paths.target.exists():
        shutil.copytree(paths.target, backup_plugin, symlinks=True)
    if paths.skill_target.exists():
        shutil.copytree(paths.skill_target, backup_skill, symlinks=True)
    if paths.runtime_config.exists():
        shutil.copy2(paths.runtime_config, backup_runtime)
    if paths.hermes_config.exists():
        shutil.copy2(paths.hermes_config, backup_config)
    _atomic_write(
        paths.backup_root / "manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
    )
    try:
        if paths.target.exists():
            shutil.rmtree(paths.target)
        os.replace(staging, paths.target)
        staging = None
        if paths.skill_target.exists():
            shutil.rmtree(paths.skill_target)
        os.replace(skill_staging, paths.skill_target)
        skill_staging = None
        _atomic_write(
            paths.runtime_config,
            (json.dumps(runtime, ensure_ascii=False, indent=2) + "\n").encode(),
        )
        previous_mode = (
            stat.S_IMODE(paths.hermes_config.stat().st_mode)
            if paths.hermes_config.exists()
            else 0o600
        )
        _atomic_write(
            paths.hermes_config,
            yaml.safe_dump(enabled_config, sort_keys=False, allow_unicode=True).encode(),
            mode=previous_mode,
        )
    except Exception as exc:
        _restore_from_manifest(paths.backup_root)
        raise HermesDeployError("plugin installation failed and was rolled back") from exc
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if skill_staging is not None and skill_staging.exists():
            shutil.rmtree(skill_staging, ignore_errors=True)
    result = doctor(hermes_home=paths.hermes_home)
    if not result["ready_for_restart"]:
        _restore_from_manifest(paths.backup_root)
        raise HermesDeployError("installed plugin failed post-install verification")
    return {
        "installed": True,
        "plugin": PLUGIN_NAME,
        "skill": SKILL_NAME,
        "backup": str(paths.backup_root),
        "requires_gateway_restart": True,
        "doctor": result,
    }


def _restore_from_manifest(backup_root: str | Path) -> dict[str, Any]:
    root = _absolute(backup_root, "backup_root")
    if root.is_symlink():
        raise HermesDeployError("rollback root cannot be a symlink")
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HermesDeployError("invalid rollback manifest") from exc
    schema_version = manifest.get("schema_version")
    if schema_version not in {1, 2}:
        raise HermesDeployError("unsupported rollback manifest version")
    home = _absolute(manifest["hermes_home"], "hermes_home")
    if root.parent != home / "k3-support" / "deploy-backups":
        raise HermesDeployError("rollback directory is outside its Hermes home")
    target = _absolute(manifest["target"], "target")
    skill_target = (
        _absolute(manifest["skill_target"], "skill_target")
        if schema_version == 2
        else None
    )
    runtime = _absolute(manifest["runtime_config"], "runtime_config")
    config = _absolute(manifest["hermes_config"], "hermes_config")
    expected = (
        home / "plugins" / PLUGIN_NAME,
        home / "k3-support" / "control-plugin.json",
        home / "config.yaml",
    )
    if (target, runtime, config) != expected:
        raise HermesDeployError("rollback manifest contains unexpected targets")
    if skill_target is not None and skill_target != (
        home / "skills" / "software-development" / SKILL_NAME
    ):
        raise HermesDeployError("rollback manifest contains unexpected skill target")
    protected = [target, runtime, config]
    if skill_target is not None:
        protected.append(skill_target)
    for protected_path in protected:
        if protected_path.is_symlink():
            raise HermesDeployError(
                f"refusing to restore over symlinked path: {protected_path}"
            )
    if target.exists():
        shutil.rmtree(target)
    if manifest["plugin_existed"]:
        shutil.copytree(root / "plugin", target, symlinks=True)
    if skill_target is not None:
        if skill_target.exists():
            shutil.rmtree(skill_target)
        if manifest["skill_existed"]:
            shutil.copytree(root / "skill", skill_target, symlinks=True)
    if manifest["runtime_existed"]:
        shutil.copy2(root / "control-plugin.json", runtime)
    else:
        runtime.unlink(missing_ok=True)
    if manifest["config_existed"]:
        shutil.copy2(root / "config.yaml", config)
    else:
        config.unlink(missing_ok=True)
    return {"restored": True, "backup": str(root)}


def rollback_plugin(*, backup_root: str | Path) -> dict[str, Any]:
    return _restore_from_manifest(backup_root)


def doctor(*, hermes_home: str | Path) -> dict[str, Any]:
    paths = deployment_paths(hermes_home)
    problems: list[str] = []
    hashes: dict[str, str] = {}
    source = _plugin_source()
    for name in PLUGIN_FILES:
        item = paths.target / name
        if not item.is_file():
            problems.append(f"missing plugin file: {name}")
        else:
            hashes[name] = _sha256(item)
            expected = hashlib.sha256(source.joinpath(name).read_bytes()).hexdigest()
            if hashes[name] != expected:
                problems.append(f"plugin file differs from package: {name}")
    skill_hashes: dict[str, str] = {}
    skill_source = _skill_source()
    for name in SKILL_FILES:
        item = paths.skill_target / name
        if not item.is_file():
            problems.append(f"missing skill file: {name}")
        else:
            skill_hashes[name] = _sha256(item)
            expected = hashlib.sha256(skill_source.joinpath(name).read_bytes()).hexdigest()
            if skill_hashes[name] != expected:
                problems.append(f"skill file differs from package: {name}")
    runtime: dict[str, Any] | None = None
    try:
        raw_runtime = json.loads(paths.runtime_config.read_text(encoding="utf-8"))
        if not isinstance(raw_runtime, dict):
            raise HermesDeployError("runtime config root must be a mapping")
        runtime = _runtime_value(
            control_cli=raw_runtime.get("control_cli", ""),
            control_config=raw_runtime.get("control_config", ""),
            timeout_seconds=int(raw_runtime.get("timeout_seconds", 0)),
        )
        if raw_runtime.get("schema_version") != RUNTIME_SCHEMA_VERSION:
            problems.append("invalid runtime schema version")
    except (
        OSError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        HermesDeployError,
    ) as exc:
        problems.append(f"invalid runtime config: {type(exc).__name__}")
    try:
        config = _load_hermes_config(paths.hermes_config)
        plugins = config.get("plugins") or {}
        if isinstance(plugins, dict):
            enabled = plugins.get("enabled") or []
            disabled = plugins.get("disabled") or []
        else:
            enabled = []
            disabled = []
        if PLUGIN_NAME not in enabled:
            problems.append("plugin is not enabled")
        if PLUGIN_NAME in disabled:
            problems.append("plugin is explicitly disabled")
    except HermesDeployError as exc:
        problems.append(str(exc))
    return {
        "plugin": PLUGIN_NAME,
        "skill": SKILL_NAME,
        "installed": paths.target.is_dir(),
        "skill_installed": paths.skill_target.is_dir(),
        "enabled": "plugin is not enabled" not in problems
        and "plugin is explicitly disabled" not in problems,
        "ready_for_restart": not problems,
        "requires_gateway_restart": True,
        "runtime": runtime,
        "hashes": hashes,
        "skill_hashes": skill_hashes,
        "problems": problems,
    }
