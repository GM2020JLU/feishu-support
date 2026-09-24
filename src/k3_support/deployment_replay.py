from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import Config
from .hermes_deploy import deployment_plan as hermes_plan
from .ids import canonical_json, digest
from .systemd_deploy import deployment_plan as systemd_plan
from .timeutil import iso_now


class DeploymentReplayError(ValueError):
    pass


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _safe_policy(config: Config) -> dict[str, Any]:
    from .feature_settings import observation

    raw = config.raw
    observed = observation(config)
    if observed["state"] == "unavailable":
        raise DeploymentReplayError("effective feature settings cannot be verified")
    policy = {
        "mode": raw["mode"],
        "timezone": raw["timezone"],
        "work_hours": config.work_hours,
        "features": observed["values"],
        "base_features": raw["features"],
        "feature_settings_state": observed["state"],
        "feature_settings_revision": observed["revision"],
        "notifications": raw["notifications"],
        "routing": raw["routing"],
        "coordination": raw["coordination"],
        "ingress": raw["ingress"],
        "mail_limits": {
            key: value
            for key, value in raw["mail"].items()
            if key != "summary_share_chat_id"
        },
        "policy": raw["policy"],
        "repositories": {
            name: {
                "base_branch": value["base_branch"],
                "build_component": value.get("build_component"),
            }
            for name, value in sorted(raw["repositories"].items())
        },
        "scope_counts": {
            "technical_chat_ids": len(raw["scope"]["technical_chat_ids"]),
            "auto_reply_chat_ids": len(raw["scope"]["auto_reply_chat_ids"]),
        },
        "identity_presence": {
            key: value is not None for key, value in sorted(raw["identity"].items())
        },
    }
    # A captured snapshot must not alias the mutable in-memory Config object.
    return json.loads(canonical_json(policy))


def capture_snapshot(
    config: Config,
    *,
    control_cli: str | Path,
    unit_dir: str | Path,
    hermes_home: str | Path,
    service_path: str | None = None,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    cli = Path(control_cli).expanduser().resolve()
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise DeploymentReplayError("control_cli is unavailable")
    systemd = systemd_plan(
        unit_dir=unit_dir,
        control_cli=cli,
        control_config=config.path,
        service_path=service_path,
    )
    hermes = hermes_plan(
        hermes_home=hermes_home,
        control_cli=cli,
        control_config=config.path,
        timeout_seconds=timeout_seconds,
    )
    body = {
        "schema_version": 1,
        "policy": _safe_policy(config),
        "artifacts": {
            "control_cli_sha256": _sha256(cli),
            "systemd_unit_hashes": systemd["hashes"],
            "hermes_plugin_hashes": hermes["source_hashes"],
            "hermes_skill_hashes": hermes["skill_source_hashes"],
        },
        "deployment_contract": {
            "systemd_unit_count": systemd["unit_count"],
            "plugin": hermes["plugin"],
            "skill": hermes["skill"],
            "runtime_mode_after_install": "unchanged",
            "requires_explicit_apply": True,
        },
    }
    return {
        **body,
        "captured_at": iso_now(),
        "snapshot_digest": digest(body),
    }


def write_snapshot(snapshot: dict[str, Any], output: str | Path) -> Path:
    target = Path(output).expanduser().resolve()
    if target == Path(target.anchor):
        raise DeploymentReplayError("snapshot output cannot be a filesystem root")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_snapshot(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentReplayError("snapshot is unreadable") from exc
    required = {
        "schema_version",
        "policy",
        "artifacts",
        "deployment_contract",
        "captured_at",
        "snapshot_digest",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["schema_version"] != 1
    ):
        raise DeploymentReplayError("snapshot schema is invalid")
    body = {
        key: value[key] for key in required - {"captured_at", "snapshot_digest"}
    }
    if value["snapshot_digest"] != digest(body):
        raise DeploymentReplayError("snapshot digest does not match content")
    return value


def compare_snapshot(
    expected: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    changed = []
    for key in ("policy", "artifacts", "deployment_contract"):
        if canonical_json(expected[key]) != canonical_json(current[key]):
            changed.append(key)
    return {
        "matches": not changed,
        "changed_sections": changed,
        "expected_digest": expected["snapshot_digest"],
        "current_digest": current["snapshot_digest"],
        "apply_performed": False,
        "next_steps": [
            "review changed_sections",
            "run systemd-install --apply",
            "run hermes-plugin-install --apply",
            "run init-db and deployment doctors",
            "keep runtime in observe until canary passes",
        ],
    }
