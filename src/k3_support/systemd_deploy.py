from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import load_config

SERVICE_ENTRYPOINTS = {
    "k3-support-ingress.service": "k3-support-ingress",
    "k3-support-worker.service": "k3-support-worker",
    "k3-support-job-worker.service": "k3-support-job-supervisor",
    "k3-support-outbox.service": "k3-support-outbox",
    "k3-support-reconcile.service": "k3-support-reconcile",
    "k3-support-body-retention.service": "k3-support-body-retention",
    "k3-support-draft-retention.service": "k3-support-draft-retention",
}
TIMER_NAMES = (
    "k3-support-reconcile.timer",
    "k3-support-backup.timer",
    "k3-support-retention.timer",
    "k3-support-mail-noon.timer",
    "k3-support-mail-evening.timer",
    "k3-support-mail-catalog.timer",
    "k3-support-source-refresh.timer",
)
UNIT_NAMES = tuple(SERVICE_ENTRYPOINTS) + (
    "k3-support-backup.service",
    "k3-support-retention.service",
    "k3-support-mail-noon.service",
    "k3-support-mail-evening.service",
    "k3-support-mail-catalog.service",
    "k3-support-source-refresh.service",
) + TIMER_NAMES


class SystemdDeployError(RuntimeError):
    pass


def _absolute(path: str | Path, label: str) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        raise SystemdDeployError(f"{label} must be an absolute path")
    if value == Path(value.anchor):
        raise SystemdDeployError(f"{label} cannot be a filesystem root")
    return value


def _unit_dir(path: str | Path) -> Path:
    value = _absolute(path, "unit_dir")
    if len(value.parts) < 2 or value.parts[-2:] != ("systemd", "user"):
        raise SystemdDeployError("unit_dir must end with systemd/user")
    return value


def _quote(value: str) -> str:
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise SystemdDeployError("systemd argument is empty or contains control bytes")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _service_path(bin_dir: Path, raw_path: str | None) -> str:
    values = [str(bin_dir)]
    for item in (raw_path or os.environ.get("PATH") or "").split(os.pathsep):
        if not item:
            continue
        candidate = Path(item).expanduser()
        if not candidate.is_absolute() or any(ch in item for ch in "\n\r\x00"):
            continue
        value = str(candidate)
        if value not in values:
            values.append(value)
    for fallback in ("/usr/local/bin", "/usr/bin", "/bin"):
        if fallback not in values:
            values.append(fallback)
    return os.pathsep.join(values)


def _service(
    *,
    description: str,
    exec_start: list[str],
    path_value: str,
    after: str | None = None,
    wants: str | None = None,
    simple: bool = False,
    install: bool = False,
) -> str:
    unit = ["[Unit]", f"Description={description}"]
    if after:
        unit.append(f"After={after}")
    if wants:
        unit.append(f"Wants={wants}")
    service = [
        "",
        "[Service]",
        f"Type={'simple' if simple else 'oneshot'}",
        "ExecStart=" + " ".join(_quote(part) for part in exec_start),
    ]
    if simple:
        service.extend(("Restart=on-failure", "RestartSec=5", "TimeoutStopSec=30"))
    service.extend(
        (
            "Environment=PYTHONUNBUFFERED=1",
            f"Environment={_quote('PATH=' + path_value)}",
            "UMask=0077",
            "NoNewPrivileges=true",
        )
    )
    if install:
        service.extend(("", "[Install]", "WantedBy=default.target"))
    return "\n".join((*unit, *service, ""))


def _timer(description: str, lines: list[str]) -> str:
    return "\n".join(
        (
            "[Unit]",
            f"Description={description}",
            "",
            "[Timer]",
            *lines,
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        )
    )


def render_units(
    *,
    control_cli: str | Path,
    control_config: str | Path,
    service_path: str | None = None,
) -> dict[str, str]:
    # Resolve convenience symlinks before rendering. Units stay pinned to an
    # exact release, while doctor gives the same answer through either path.
    cli = _absolute(control_cli, "control_cli").resolve()
    config_path = _absolute(control_config, "control_config").resolve()
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise SystemdDeployError("control_cli does not exist or is not executable")
    if not config_path.is_file():
        raise SystemdDeployError("control_config does not exist")
    config = load_config(config_path)
    bin_dir = cli.parent
    entrypoints = {name: bin_dir / executable for name, executable in SERVICE_ENTRYPOINTS.items()}
    for executable in [cli, *entrypoints.values()]:
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise SystemdDeployError(f"required entrypoint is unavailable: {executable.name}")
    path_value = _service_path(bin_dir, service_path)
    config_arg = str(config_path)
    units = {
        "k3-support-draft-retention.service": _service(
            description="K3 support opt-in captured draft retention",
            exec_start=[str(entrypoints["k3-support-draft-retention.service"]), "--config", config_arg],
            path_value=path_value,
            simple=True,
            install=True,
        ),
        "k3-support-body-retention.service": _service(
            description="K3 support opt-in database body retention",
            exec_start=[str(entrypoints["k3-support-body-retention.service"]), "--config", config_arg],
            path_value=path_value,
            simple=True,
            install=True,
        ),
        "k3-support-ingress.service": _service(
            description="K3 support Feishu ingress",
            exec_start=[str(entrypoints["k3-support-ingress.service"]), "--config", config_arg],
            path_value=path_value,
            after="network-online.target",
            wants="network-online.target",
            simple=True,
            install=True,
        ),
        "k3-support-worker.service": _service(
            description="K3 support orchestrator worker",
            exec_start=[str(entrypoints["k3-support-worker.service"]), "--config", config_arg],
            path_value=path_value,
            after="k3-support-ingress.service",
            simple=True,
            install=True,
        ),
        "k3-support-job-worker.service": _service(
            description="K3 support supervised job worker",
            exec_start=[str(entrypoints["k3-support-job-worker.service"]), "--config", config_arg],
            path_value=path_value,
            after="network-online.target k3-support-worker.service",
            wants="network-online.target",
            simple=True,
            install=True,
        ),
        "k3-support-outbox.service": _service(
            description="K3 support transactional Outbox sender",
            exec_start=[str(entrypoints["k3-support-outbox.service"]), "--config", config_arg],
            path_value=path_value,
            after="network-online.target",
            wants="network-online.target",
            simple=True,
            install=True,
        ),
        "k3-support-reconcile.service": _service(
            description="K3 support reconciliation and crash recovery",
            exec_start=[
                str(entrypoints["k3-support-reconcile.service"]),
                "--config",
                config_arg,
                "--once",
            ],
            path_value=path_value,
        ),
        "k3-support-backup.service": _service(
            description="K3 support verified SQLite backup",
            exec_start=[str(cli), "--config", config_arg, "backup"],
            path_value=path_value,
        ),
        "k3-support-retention.service": _service(
            description="K3 support raw artifact retention",
            exec_start=[str(cli), "--config", config_arg, "retention", "--apply"],
            path_value=path_value,
        ),
        "k3-support-mail-noon.service": _service(
            description="K3 support noon mail summary",
            exec_start=[str(cli), "--config", config_arg, "mail-summary", "--slot", "mail_noon"],
            path_value=path_value,
        ),
        "k3-support-mail-evening.service": _service(
            description="K3 support evening mail summary",
            exec_start=[
                str(cli),
                "--config",
                config_arg,
                "mail-summary",
                "--slot",
                "mail_evening",
            ],
            path_value=path_value,
        ),
        "k3-support-source-refresh.service": _service(
            description="K3 support knowledge source freshness refresh",
            exec_start=[
                str(cli), "--config", config_arg, "knowledge-source-refresh",
                "--max-age-hours", "24",
            ],
            path_value=path_value,
        ),
        "k3-support-mail-catalog.service": _service(
            description="K3 support resumable mail catalog scan",
            exec_start=[
                str(cli),
                "--config",
                config_arg,
                "mail-catalog-scan",
                "--max-pages",
                "1",
                "--page-size",
                "100",
            ],
            path_value=path_value,
            after="network-online.target",
            wants="network-online.target",
        ),
        "k3-support-reconcile.timer": _timer(
            "Run K3 support reconciliation every five minutes",
            ["OnBootSec=2min", "OnUnitActiveSec=5min", "Persistent=true", "RandomizedDelaySec=15s"],
        ),
        "k3-support-backup.timer": _timer(
            "Daily K3 support database backup",
            [
                f"OnCalendar=*-*-* 02:30:00 {config.raw['timezone']}",
                "Persistent=true",
                "RandomizedDelaySec=2min",
            ],
        ),
        "k3-support-retention.timer": _timer(
            "Daily K3 support retention enforcement",
            [
                f"OnCalendar=*-*-* 02:45:00 {config.raw['timezone']}",
                "Persistent=true",
                "RandomizedDelaySec=2min",
            ],
        ),
        "k3-support-mail-noon.timer": _timer(
            "Run K3 support mail summary at noon",
            [
                f"OnCalendar=*-*-* 12:00:00 {config.raw['timezone']}",
                "Persistent=true",
                "AccuracySec=1s",
            ],
        ),
        "k3-support-mail-evening.timer": _timer(
            "Run K3 support mail summary in the evening",
            [
                f"OnCalendar=*-*-* 18:00:00 {config.raw['timezone']}",
                "Persistent=true",
                "AccuracySec=1s",
            ],
        ),
        "k3-support-source-refresh.timer": _timer(
            "Refresh K3 support knowledge sources daily",
            [
                f"OnCalendar=*-*-* 03:15:00 {config.raw['timezone']}",
                "Persistent=true",
                "RandomizedDelaySec=5min",
            ],
        ),
        "k3-support-mail-catalog.timer": _timer(
            "Continue K3 support historical mail catalog scan",
            [
                "OnBootSec=10min",
                "OnUnitInactiveSec=10min",
                "Persistent=true",
                "RandomizedDelaySec=30s",
            ],
        ),
    }
    if set(units) != set(UNIT_NAMES):
        raise SystemdDeployError("rendered unit set does not match the deployment contract")
    return units


def _hashes(units: dict[str, str]) -> dict[str, str]:
    return {
        name: hashlib.sha256(content.encode()).hexdigest()
        for name, content in sorted(units.items())
    }


def deployment_plan(
    *,
    unit_dir: str | Path,
    control_cli: str | Path,
    control_config: str | Path,
    service_path: str | None = None,
) -> dict[str, Any]:
    target = _unit_dir(unit_dir)
    units = render_units(
        control_cli=control_cli,
        control_config=control_config,
        service_path=service_path,
    )
    return {
        "unit_dir": str(target),
        "unit_count": len(units),
        "would_replace": sorted(name for name in units if (target / name).exists()),
        "hashes": _hashes(units),
        "apply_required": True,
        "daemon_reload_required": True,
        "services_started": False,
    }


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_units(
    *,
    unit_dir: str | Path,
    control_cli: str | Path,
    control_config: str | Path,
    service_path: str | None = None,
) -> dict[str, Any]:
    target = _unit_dir(unit_dir)
    config = load_config(_absolute(control_config, "control_config"))
    units = render_units(
        control_cli=control_cli,
        control_config=control_config,
        service_path=service_path,
    )
    target.mkdir(parents=True, exist_ok=True)
    for name in units:
        path = target / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise SystemdDeployError(f"refusing to replace unsafe unit path: {path}")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = config.data_dir / "deploy-backups" / "systemd" / timestamp
    backup.mkdir(parents=True, mode=0o700)
    existed = []
    for name in units:
        path = target / name
        if path.exists():
            existed.append(name)
            shutil.copy2(path, backup / name)
    manifest = {
        "schema_version": 1,
        "unit_dir": str(target),
        "data_dir": str(config.data_dir.resolve()),
        "units": sorted(units),
        "existed": sorted(existed),
    }
    (backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(backup / "manifest.json", 0o600)
    try:
        for name, content in units.items():
            _atomic_write(target / name, content)
    except Exception as exc:
        _restore(backup)
        raise SystemdDeployError("unit installation failed and was rolled back") from exc
    result = doctor(
        unit_dir=target,
        control_cli=control_cli,
        control_config=control_config,
        service_path=service_path,
    )
    if not result["ready_for_reload"]:
        _restore(backup)
        raise SystemdDeployError("installed units failed post-install verification")
    return {
        "installed": True,
        "backup": str(backup),
        "doctor": result,
        "daemon_reload_required": True,
        "services_started": False,
    }


def _restore(backup_root: str | Path) -> dict[str, Any]:
    root = _absolute(backup_root, "backup_root")
    if root.is_symlink():
        raise SystemdDeployError("rollback root cannot be a symlink")
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemdDeployError("invalid systemd rollback manifest") from exc
    if manifest.get("schema_version") != 1:
        raise SystemdDeployError("unsupported systemd rollback manifest")
    target = _unit_dir(manifest["unit_dir"])
    data_dir = _absolute(manifest["data_dir"], "data_dir")
    if root.parent != data_dir / "deploy-backups" / "systemd":
        raise SystemdDeployError("rollback directory is outside the configured data dir")
    units = manifest.get("units")
    existed = manifest.get("existed")
    if (
        not isinstance(units, list)
        or set(units) != set(UNIT_NAMES)
        or not isinstance(existed, list)
        or not set(existed) <= set(units)
    ):
        raise SystemdDeployError("rollback manifest contains an invalid unit set")
    for name in units:
        path = target / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise SystemdDeployError(f"refusing to restore unsafe unit path: {path}")
    for name in units:
        path = target / name
        if name in existed:
            shutil.copy2(root / name, path)
        else:
            path.unlink(missing_ok=True)
    return {"restored": True, "backup": str(root), "daemon_reload_required": True}


def rollback_units(*, backup_root: str | Path) -> dict[str, Any]:
    return _restore(backup_root)


def doctor(
    *,
    unit_dir: str | Path,
    control_cli: str | Path,
    control_config: str | Path,
    service_path: str | None = None,
) -> dict[str, Any]:
    target = _unit_dir(unit_dir)
    expected = render_units(
        control_cli=control_cli,
        control_config=control_config,
        service_path=service_path,
    )
    expected_hashes = _hashes(expected)
    actual_hashes: dict[str, str] = {}
    problems: list[str] = []
    for name in UNIT_NAMES:
        path = target / name
        if not path.is_file() or path.is_symlink():
            problems.append(f"missing or unsafe unit: {name}")
            continue
        actual_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hashes[name] != expected_hashes[name]:
            problems.append(f"unit differs from rendered contract: {name}")
    return {
        "unit_dir": str(target),
        "unit_contract_ready": not problems,
        "ready_for_reload": not problems,
        "problems": problems,
        "hashes": actual_hashes,
        # This doctor compares files only. It intentionally does not claim to
        # know whether systemd has reloaded them or whether services are live.
        "runtime_state_checked": False,
        "daemon_reload_required": None,
        "services_started": None,
    }
