#!/usr/bin/env python3
"""Offline, non-deploying wheel acceptance in a new, private artifact directory.

The controller imports only the standard library. Runtime probes use the installed
interpreter with -I, outside the checkout, with network/subprocess audit guards.
An existing uv cache (or --wheelhouse) must already contain all dependencies.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path


class VerificationError(RuntimeError):
    pass


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def wheelhouse_manifest(directory: Path) -> dict[str, str]:
    """A release input is a bounded set of regular wheels, never a live index."""
    result = {}
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
            raise VerificationError("wheelhouse must contain only regular .whl files")
        result[path.name] = sha256(path.read_bytes())
    if not result:
        raise VerificationError("wheelhouse is empty")
    return result


def locked_constraints(source: Path) -> str:
    lock = tomllib.loads((source / "uv.lock").read_text())
    versions = {}
    for package in lock["package"]:
        if "registry" not in package.get("source", {}):
            continue
        name, version = package["name"], package["version"]
        if name in versions and versions[name] != version:
            raise VerificationError("multiple locked versions need platform-specific export")
        versions[name] = version
    if not versions:
        raise VerificationError("runtime lock is empty")
    return "".join(f"{name}=={version}\n" for name, version in sorted(versions.items()))


def verify_dependency_hashes(source: Path, manifest: dict[str, str]) -> None:
    lock = tomllib.loads((source / "uv.lock").read_text())
    allowed = {wheel["hash"].removeprefix("sha256:")
               for package in lock["package"] for wheel in package.get("wheels", [])}
    allowed.update(re.findall(r"--hash=sha256:([a-f0-9]{64})",
                              (source / "config/build-requirements.lock").read_text()))
    if any(digest not in allowed for digest in manifest.values()):
        raise VerificationError("wheelhouse contains a wheel not covered by dependency locks")


def source_manifest(source: Path) -> dict[str, str]:
    package = source / "src/k3_support"
    if not package.is_dir() or not (source / "pyproject.toml").is_file():
        raise VerificationError("source must contain pyproject.toml and src/k3_support")
    result = {}
    for path in sorted(package.rglob("*")):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise VerificationError(f"package source symlinks are not accepted: {path}")
        if path.is_file():
            result[path.relative_to(package.parent).as_posix()] = sha256(path.read_bytes())
    return result


def reserve_output(output: Path, source: Path) -> Path:
    """Never overwrite, follow a symlink, or write into an arbitrary live tree."""
    output = output.expanduser().absolute()
    if any(part.is_symlink() for part in (output, *output.parents)):
        raise VerificationError("output path must not contain symlinks")
    output = output.resolve()
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    allowed = (Path(tempfile.gettempdir()).resolve(), (data / "codex/artifacts").resolve())
    if not any(output != root and output.is_relative_to(root) for root in allowed):
        raise VerificationError("output must be below the temporary directory or XDG_DATA_HOME/codex/artifacts")
    if output.is_relative_to(source.resolve()) or source.resolve().is_relative_to(output):
        raise VerificationError("output must be outside the source checkout")
    if output.exists():
        raise VerificationError("output directory already exists; choose a new directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    return output


def write_new(path: Path, value: str, *, executable: bool = False) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(value)
    path.chmod(0o700 if executable else 0o600)


def private_environment(output: Path, *, path: str) -> dict[str, str]:
    """Allowlist, rather than blacklist, inherited settings and credentials."""
    return {
        "HOME": str(output / "sandbox-home"),
        "PATH": path,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "XDG_CONFIG_HOME": str(output / "xdg-config"),
        "XDG_DATA_HOME": str(output / "xdg-data"),
        "XDG_CACHE_HOME": str(output / "xdg-cache"),
        "XDG_STATE_HOME": str(output / "xdg-state"),
        "XDG_RUNTIME_DIR": str(output / "xdg-runtime"),
        "TMPDIR": str(output / "tmp"),
        "HERMES_HOME": str(output / "sandbox-hermes"),
        "UV_OFFLINE": "true",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_KEYRING_PROVIDER": "disabled",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def verify_wheel(wheel: Path, expected: dict[str, str]) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise VerificationError("wheel has duplicate or unsafe members")
        actual = {name: sha256(archive.read(name)) for name in names if name.startswith("k3_support/") and not name.endswith("/")}
    if actual != expected:
        differences = sorted(name for name in actual.keys() | expected.keys() if actual.get(name) != expected.get(name))
        raise VerificationError(f"wheel/source package parity failed: {differences[:12]}")
    return actual


GUARD = '''"""Installed smoke only: reject accidental transport or command execution."""
import sys
sys.dont_write_bytecode = True
def _offline_guard(event, args):
    if event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"} or event.startswith("socket."):
        raise PermissionError("offline installed smoke forbids " + event)
sys.addaudithook(_offline_guard)
K3_INSTALLED_SMOKE_GUARD = True
'''


PROBE = r'''
import hashlib, importlib, importlib.metadata, importlib.resources, json, pathlib, sqlite3, sys
import sitecustomize
assert sitecustomize.K3_INSTALLED_SMOKE_GUARD is True
root = pathlib.Path(sys.argv[1]).resolve()
expected = json.loads((root / "expected.json").read_text())
venv = (root / "venv").resolve()
source = pathlib.Path(expected["source"]).resolve()
assert sys.flags.isolated and pathlib.Path(sys.prefix).resolve() == venv
assert not any(pathlib.Path(value).resolve().is_relative_to(source) for value in sys.path if value)
origins = {}
for name in ("k3_support", "k3_support.cli", "k3_support.services", "k3_support.hermes_plugin", "k3_support.db"):
    module = importlib.import_module(name)
    origin = pathlib.Path(module.__file__).resolve()
    assert origin.is_relative_to(venv) and not origin.is_relative_to(source), (name, origin)
    origins[name] = str(origin)
dist = importlib.metadata.distribution("k3-support")
assert dist.version == expected["version"]
entries = {entry.name: entry.value for entry in dist.entry_points if entry.group == "console_scripts"}
assert entries == expected["entry_points"], entries
for name, value in entries.items():
    console = venv / "bin" / name
    assert console.is_file() and console.read_text().startswith("#!")
    module, attr = value.split(":")
    assert callable(getattr(importlib.import_module(module), attr))
for relative, wanted in expected["files"].items():
    path = pathlib.Path(dist.locate_file(relative)).resolve()
    assert path.is_relative_to(venv) and not path.is_relative_to(source), path
    assert hashlib.sha256(path.read_bytes()).hexdigest() == wanted, relative
from k3_support import db
from k3_support.config import load_config
from k3_support.runtime_control import ensure_global_state
from k3_support.store import create_case, ingest_event, enqueue_outbox
from k3_support.hermes_plugin import _is_control_message, pre_gateway_dispatch
assert _is_control_message("/feishu") and not _is_control_message("/k3")
assert callable(pre_gateway_dispatch)
resources = importlib.resources.files("k3_support")
for relative in expected["files"]:
    if "/schemas/" in relative:
        json.loads(resources.joinpath(relative.removeprefix("k3_support/")).read_text())
migrations = db.migration_files()
assert [name for _, name, _ in migrations] == expected["migrations"]
config = load_config(root / "fixture-config.yaml")
assert config.raw["schema_version"] == 2 and config.feature("mail") and config.feature("shadow_reply")
assert not any(config.feature(feature) for feature in ("board", "codex", "wip_push"))
assert config.raw["runtime"]["remote_host"] is None and not config.raw["repositories"]
assert not config.notification("feishu_app_urgent") and not config.notification("feishu_sms_urgent")
connection = db.connect(config.database_path)
applied = db.migrate(connection)
assert applied == [version for version, _, _ in migrations]
ensure_global_state(connection, external_id="offline-fixture-init")
artifact = config.data_dir / 'attachments' / 'fixture-source.txt'
artifact.parent.mkdir(parents=True, exist_ok=True)
artifact.write_bytes(b'synthetic attachment; never a user document')
event, _ = ingest_event(connection, source="feishu_user_poll", identity="user", external_id="fixture-message",
                       payload={"text": "synthetic acceptance fixture"}, occurred_at="2026-09-01T00:00:00+00:00",
                       raw_artifact_path=str(artifact))
case, _ = create_case(connection, title="Offline fixture; not a user issue", case_type="faq", severity="P2",
                      confidence=0.9, requester_id="fixture-requester", source_event_pk=event)
enqueue_outbox(connection, channel="telegram", action_type="owner_notification", destination="telegram:fixture-chat",
               payload={"text": "synthetic fixture; never delivered"}, idempotency_key="fixture-outbox", case_id=case)
from k3_support.approvals import request_approval
request_approval(connection, approval_type='board1_lease', case_id=case,
                 action={'synthetic': True}, session_id='fixture-never-executed',
                 expires_at='2099-01-01T00:00:00+00:00')
connection.execute("UPDATE approvals SET status='approved'")
# Synthetic outstanding authority only; no consumer or physical process exists.
connection.execute("""INSERT INTO jobs(job_id,case_id,job_type,state,lease_owner,lease_expires_at,
    input_digest,attempt_no,available_at,created_at,updated_at,context_json)
    VALUES('fixture-job',?,'codex','running','fixture-worker','2099-01-01T00:00:00+00:00',
    ?,1,'2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00','{}')""",
    (case, 'a' * 64))
connection.execute("""INSERT INTO broker_grants VALUES('fixture-grant',?,65534,'fixture-job',1,1,?,
    'fixture-worker','2026-09-01T00:00:00+00:00','2099-01-01T00:00:00+00:00',NULL)""",
    ('b' * 64, 'a' * 64))
connection.execute("INSERT INTO locks VALUES('board1','fixture-lock',?,'board','old','future','old','{}')", (case,))
for index in range(1, expected['fixture_cases']):
    source_event, _ = ingest_event(connection, source='feishu_user_poll', identity='user',
        external_id=f'fixture-message-{index}', payload={'text': 'synthetic capacity fixture ' + 'x' * 1024},
        occurred_at='2026-09-01T00:00:00+00:00')
    create_case(connection, title=f'Offline capacity fixture {index}', case_type='faq', severity='P2',
                confidence=0.5, requester_id='fixture-requester', source_event_pk=source_event)
connection.execute("INSERT INTO watermarks VALUES(?,?,?)", ("fixture-offset", '{"cursor":"opaque-fixture"}', "2026-09-01T00:00:00+00:00"))
connection.execute("INSERT INTO feishu_control_cards VALUES('fcc_fixture','fixture-user','fixture-chat','fixture-open','om_fixture','[\"mode\"]','2099-01-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')")
assert db.integrity(connection)["ok"]
logical = hashlib.sha256("\n".join(connection.iterdump()).encode()).hexdigest()
connection.close()
# Exercise the preceding installed schema -> latest upgrade, without a live DB.
old = db.connect(root / "previous-schema.db")
original = db.migration_files
try:
    db.migration_files = lambda: migrations[:-1]
    db.migrate(old)
finally:
    db.migration_files = original
old_case, _ = create_case(old, title="Previous-schema fixture", case_type="faq", severity="P2", confidence=0.5)
old_before = dict(old.execute("SELECT * FROM cases WHERE case_id=?", (old_case,)).fetchone())
assert db.migrate(old) == [migrations[-1][0]]
assert dict(old.execute("SELECT * FROM cases WHERE case_id=?", (old_case,)).fetchone()) == old_before
assert db.integrity(old)["ok"]
old.close()
# A guard self-test fails before any real network or subprocess operation.
import socket, subprocess
for operation in (lambda: socket.socket(), lambda: subprocess.run(["must-not-execute"])):
    try:
        operation()
    except PermissionError as error:
        assert "offline installed smoke forbids" in str(error)
    else:
        raise AssertionError("runtime side-effect guard is inactive")
print(json.dumps({"origins": origins, "version": dist.version, "entry_points": entries,
                  "migrations": [version for version, _, _ in migrations], "case_id": case,
                  "logical_digest": logical, "guard_verified": True, "previous_schema_upgrade": True}))
'''


CHECK_STATE = r'''
import hashlib, json, pathlib, sqlite3, sys
root = pathlib.Path(sys.argv[1])
path = pathlib.Path(sys.argv[2])
conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
value = hashlib.sha256("\n".join(conn.iterdump()).encode()).hexdigest()
conn.close()
print(json.dumps({"logical_digest": value}))
'''


RESTORE = r'''
import hashlib, json, pathlib, sqlite3, sys
from k3_support.operations import restore_probe
root, backup = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
target = root / "restored.db"
source = sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)
dest = sqlite3.connect(target)
source.backup(dest)
source.close()
before = hashlib.sha256("\n".join(dest.iterdump()).encode()).hexdigest()
dest.execute("UPDATE cases SET title='restore mutation probe'")
dest.commit()
changed = hashlib.sha256("\n".join(dest.iterdump()).encode()).hexdigest()
assert before != changed
source = sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)
source.backup(dest)
source.close()
restored = hashlib.sha256("\n".join(dest.iterdump()).encode()).hexdigest()
dest.close()
assert before == restored
probe = restore_probe(target)
expected = json.loads((root / 'expected.json').read_text())
assert probe["integrity"]["ok"] and probe["counts"]["cases"] == expected['fixture_cases'] and probe["counts"]["outbox"] == 1
print(json.dumps({"logical_digest": restored, "mutation_observed": True, "probe": probe}))
'''


def fixture_config(output: Path) -> dict:
    return {
        "schema_version": 2, "mode": "shadow", "timezone": "Asia/Kathmandu",
        "work_hours": {"start": "22:15", "end": "06:45"},
        "paths": {"data_dir": str(output / "instance"), "database": str(output / "instance/state/support.db")},
        "features": {name: name in {"shadow_reply", "mail"} for name in
                     ("shadow_reply", "auto_faq", "codex", "board", "wip_push", "mail", "calendar", "base_sync")},
        "notifications": {name: False for name in ("telegram_p0", "feishu_p0_message", "feishu_app_urgent", "feishu_sms_urgent")},
        "runtime": {"lark_cli_command": str(output / "stub-bin/lark-cli"), "hermes_command": str(output / "stub-bin/hermes")},
        "policy": {"auto_reply_confidence": 0.85, "raw_retention_days": 30, "board_alias": "board1",
                   "board_lease_max_minutes": 240, "push_approval_minutes": 30},
        "identity": {"telegram_control_user_id": "fixture-user", "telegram_control_chat_id": "fixture-chat",
                     "feishu_owner_open_id": "fixture-owner", "feishu_p0_chat_id": None},
        "scope": {"technical_chat_ids": ["fixture-support-chat"], "auto_reply_chat_ids": []},
        "repositories": {}, "base": {name: None for name in
                                     ("app_token", "cases_table_id", "knowledge_table_id", "mail_table_id", "health_table_id")},
    }


WORKFLOW_RESTORE = r'''
import hashlib, json, pathlib, sqlite3, sys
from k3_support.config import load_config, FEATURES
from k3_support.recovery_bundle import create
from k3_support.recovery_stage import stage
root = pathlib.Path(sys.argv[1])
config = load_config(root / 'fixture-config.yaml')
def logical(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        return hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()
    finally:
        db.close()
before = logical(config.database_path)
bundle, target = root / 'workflow-bundle', root / 'workflow-restored'
create(config, bundle)
result = stage(bundle, target)
assert result['staged'] and not result['activation_allowed']
assert logical(config.database_path) == before
assert (target / 'data/attachments/fixture-source.txt').read_bytes() == b'synthetic attachment; never a user document'
db = sqlite3.connect(target / 'database.db')
assert db.execute('SELECT mode FROM global_control_state').fetchone()[0] == 'stopped'
assert db.execute('SELECT status FROM approvals').fetchone()[0] == 'revoked'
assert db.execute('SELECT state FROM outbox').fetchone()[0] == 'cancelled'
assert db.execute('SELECT state FROM jobs').fetchone()[0] == 'orphaned'
assert db.execute('SELECT revoked_at FROM broker_grants').fetchone()[0]
assert db.execute("SELECT julianday(expires_at)<=julianday('now') FROM feishu_control_cards WHERE card_id='fcc_fixture'").fetchone()[0] == 1
assert db.execute("SELECT delivered_message_id FROM feishu_control_cards WHERE card_id='fcc_fixture'").fetchone()[0] == 'om_fixture'
assert db.execute("SELECT owner FROM locks WHERE lock_key='board1'").fetchone()[0] == 'fixture-lock'
assert db.execute('SELECT raw_artifact_path FROM inbound_events WHERE raw_artifact_path IS NOT NULL').fetchone()[0] == str(target / 'data/attachments/fixture-source.txt')
assert not list(db.execute('PRAGMA foreign_key_check'))
db.close()
review = load_config(target / 'config.review.yaml')
assert review.mode == 'shadow' and all(not review.feature(name) for name in FEATURES)
print(json.dumps({'source_unchanged': True, 'attachment_restored': True,
                  'authority_fenced': True, 'activation_allowed': False}))
'''


class Runner:
    def __init__(self, output: Path, env: dict[str, str]):
        self.output, self.env = output, env
        self.steps: list[dict] = []

    def run(self, name: str, command: list[str], *, as_json: bool = False, expected_code: int = 0):
        started = time.monotonic()
        try:
            completed = subprocess.run(command, cwd=self.output, env=self.env, stdin=subprocess.DEVNULL,
                                       capture_output=True, text=True, timeout=180, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.steps.append({'name': name, 'returncode': None,
                               'elapsed_seconds': round(time.monotonic() - started, 6),
                               'error_class': type(exc).__name__})
            raise
        self.steps.append({"name": name, "returncode": completed.returncode,
                           'elapsed_seconds': round(time.monotonic() - started, 6)})
        write_new(self.output / f"{name}.stdout.log", completed.stdout)
        write_new(self.output / f"{name}.stderr.log", completed.stderr)
        if completed.returncode != expected_code:
            hint = "; offline dependencies must be pre-cached" if name in {"build", "install"} else ""
            raise VerificationError(f"{name} failed ({completed.returncode}); see {name}.stderr.log{hint}")
        return json.loads(completed.stdout) if as_json else completed.stdout


def verify(args: argparse.Namespace) -> dict:
    fixture_cases = getattr(args, 'fixture_cases', 1)
    if type(fixture_cases) is not int or not 1 <= fixture_cases <= 10000:
        raise VerificationError('fixture cases must be an integer from 1 to 10000')
    source = Path(args.source).expanduser().resolve()
    manifest = source_manifest(source)
    project_bytes = (source / "pyproject.toml").read_bytes()
    project = tomllib.loads(project_bytes.decode())["project"]
    uv = shutil.which("uv")
    if uv is None:
        raise VerificationError("uv must already be installed")
    wheelhouse = Path(args.wheelhouse).expanduser().resolve() if args.wheelhouse else None
    if wheelhouse and not wheelhouse.is_dir():
        raise VerificationError("wheelhouse must be an existing local directory")
    clean_cache = getattr(args, "clean_cache", False)
    if clean_cache and not wheelhouse:
        raise VerificationError("clean-cache requires an explicit wheelhouse")
    supplied = Path(args.wheel).expanduser().resolve() if args.wheel else None
    if supplied and (not supplied.is_file() or supplied.suffix != ".whl"):
        raise VerificationError("wheel must be an existing local .whl file")
    output = reserve_output(Path(args.output_dir), source)
    env = private_environment(output, path="/usr/bin:/bin")
    for path in {Path(value) for key, value in env.items() if key.endswith("_HOME") or key in {"HOME", "TMPDIR", "XDG_RUNTIME_DIR"}}:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache = Path(os.environ.get("UV_CACHE_DIR", Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "uv")).expanduser().resolve()
    if clean_cache:
        cache = output / "empty-uv-cache"
        cache.mkdir(mode=0o700)
    runner = Runner(output, env)
    report = {"ok": False, "output": str(output), "source": str(source), "offline": True,
              "deployment_performed": False, "external_auth_verified": False, "steps": runner.steps,
              "uv_cache": str(cache), "scope": "synthetic installed-release acceptance, not live service readiness"}
    report["clean_cache"] = clean_cache
    try:
        uv_base = [uv, "--offline", "--no-config", "--cache-dir", str(cache)]
        find_links = ["--find-links", str(wheelhouse)] if wheelhouse else []
        constraints = []
        if clean_cache:
            report["wheelhouse"] = wheelhouse_manifest(wheelhouse)
            verify_dependency_hashes(source, report["wheelhouse"])
            report["lock_sha256"] = sha256((source / "uv.lock").read_bytes())
            build_lock = (source / "config/build-requirements.lock").read_text()
            report["build_lock_sha256"] = sha256(build_lock.encode())
            write_new(output / "build-constraints.txt", build_lock)
            write_new(output / "locked-constraints.txt", locked_constraints(source))
            constraints = ["--constraint", str(output / "locked-constraints.txt")]
            find_links += ["--no-index"]
        if supplied:
            wheel = output / supplied.name
            shutil.copyfile(supplied, wheel)
        else:
            wheels = output / "wheels"
            wheels.mkdir(mode=0o700)
            runner.run("build", [*uv_base, "build", "--wheel", "--out-dir", str(wheels),
                                 "--python", sys.executable, "--no-python-downloads", *find_links,
                                 *(["--build-constraints", str(output / "build-constraints.txt")] if clean_cache else []), str(source)])
            candidates = list(wheels.glob("*.whl"))
            if len(candidates) != 1:
                raise VerificationError("build must produce exactly one wheel")
            wheel = candidates[0]
        verify_wheel(wheel, manifest)
        if source_manifest(source) != manifest or (source / "pyproject.toml").read_bytes() != project_bytes:
            raise VerificationError("source changed while building; freeze source and retry")
        report["wheel"] = {"path": str(wheel), "sha256": sha256(wheel.read_bytes())}
        venv = output / "venv"
        runner.run("venv", [*uv_base, "venv", "--python", sys.executable, "--no-python-downloads", str(venv)])
        python = str(venv / "bin/python")
        runner.run("install", [*uv_base, "pip", "install", "--python", python, "--link-mode", "copy", *find_links, *constraints, str(wheel)])
        inventory = runner.run("package-inventory", [*uv_base, "pip", "list", "--python", python, "--format", "json"], as_json=True)
        report["package_inventory"] = inventory
        write_new(output / "sbom.cdx.json", json.dumps({
            "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
            "components": [{"type": "library", "name": item["name"],
                            "version": item["version"],
                            "purl": f"pkg:pypi/{item['name']}@{item['version']}"}
                           for item in inventory],
        }, indent=2))
        site_dir = Path(runner.run("site-path", [python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).strip())
        if not site_dir.resolve().is_relative_to(venv):
            raise VerificationError("venv site-packages escaped the isolated environment")
        write_new(site_dir / "sitecustomize.py", GUARD)
        (output / "stub-bin").mkdir(mode=0o700)
        for name in ("lark-cli", "hermes"):
            write_new(output / "stub-bin" / name, "#!/bin/sh\nexit 97\n", executable=True)
        runner.env = private_environment(output, path=f"{output / 'stub-bin'}:{venv / 'bin'}")
        expected = {"source": str(source), "files": manifest, "version": project["version"],
                    "entry_points": project["scripts"],
                    "migrations": sorted(Path(name).name for name in manifest if "/migrations/" in name and name.endswith(".sql"))}
        expected['fixture_cases'] = fixture_cases
        report['fixture_cases'] = fixture_cases
        report['fixture_scope'] = 'synthetic database rows, not production workload or RTO guarantee'
        write_new(output / "expected.json", json.dumps(expected, indent=2))
        config = output / "fixture-config.yaml"
        write_new(config, json.dumps(fixture_config(output), indent=2))
        installed = runner.run("installed-probe", [python, "-I", "-c", PROBE, str(output)], as_json=True)
        report["installed"] = installed
        cli = [python, "-I", str(venv / "bin/k3-supportctl"), "--config", str(config)]
        doctor = runner.run("runtime-doctor", [*cli, "runtime-doctor", "--check-remote"], as_json=True)
        if not doctor["ready"] or set(doctor["commands"]) != {"hermes", "semantic", "lark_cli"} or doctor["remote"]["checked"]:
            raise VerificationError("chat/mail installation unexpectedly requires board/Codex/SSH capabilities")
        report["runtime_doctor"] = doctor
        health = runner.run("health", [*cli, "health"], as_json=True)
        if not health["database"]["ok"] or health["runtime_mode"] != "observe":
            raise VerificationError("fixture health/database failed")
        status = runner.run("status", [*cli, "status", '--limit', '10'], as_json=True)
        if len(status["cases"]) != min(fixture_cases, 10):
            raise VerificationError("installed status did not read fixture state")
        control_args = ["control", "--control-user-id", "fixture-user", "--control-chat-id", "fixture-chat",
                        "--message-id", "fixture-control-read", "--text", f"status {installed['case_id']}"]
        control = runner.run("control-read", [*cli, *control_args], as_json=True)
        if control["case"]["case_id"] != installed["case_id"]:
            raise VerificationError("installed control status disagrees with the database")
        rejected = control_args.copy()
        rejected[rejected.index("fixture-user")] = "not-the-operator"
        runner.run("control-reject-identity", [*cli, *rejected], expected_code=2)
        if "control identity mismatch" not in (output / "control-reject-identity.stderr.log").read_text():
            raise VerificationError("wrong control identity did not fail for the expected authorization reason")
        database = output / "instance/state/support.db"
        state = runner.run("after-read-state", [python, "-I", "-c", CHECK_STATE, str(output), str(database)], as_json=True)
        if state["logical_digest"] != installed["logical_digest"]:
            raise VerificationError("health/control read commands changed logical fixture state")
        backup = runner.run("backup", [*cli, "backup"], as_json=True)
        probe = runner.run("restore-probe", [*cli, "restore-probe", backup["path"]], as_json=True)
        restored = runner.run("restore-roundtrip", [python, "-I", "-c", RESTORE, str(output), backup["path"]], as_json=True)
        report['workflow_restore'] = runner.run('workflow-restore',
            [python, '-I', '-c', WORKFLOW_RESTORE, str(output)], as_json=True)
        if restored["logical_digest"] != installed["logical_digest"] or not probe["integrity"]["ok"]:
            raise VerificationError("backup/restore does not preserve logical fixture state")
        report.update({"ok": True, "read_commands_preserved_state": True, "backup": backup,
                       "restore": restored, "plugin_loaded_without_hermes": True})
        if clean_cache and (wheelhouse_manifest(wheelhouse) != report["wheelhouse"]
                            or sha256((source / "uv.lock").read_bytes()) != report["lock_sha256"]
                            or sha256((source / "config/build-requirements.lock").read_bytes()) != report["build_lock_sha256"]):
            report["ok"] = False
            raise VerificationError("release dependency inputs changed during verification")
    except Exception as exc:
        report["error"] = {"class": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        write_new(output / "report.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--fixture-cases', type=int, default=1, help='synthetic backup/restore workload, 1 to 10000 cases')
    parser.add_argument("--wheel", help="verify this existing wheel instead of building")
    parser.add_argument("--wheelhouse", help="existing local dependency/build wheel directory; never downloaded")
    parser.add_argument("--clean-cache", action="store_true", help="require wheelhouse, empty private cache, runtime lock and no index")
    parser.add_argument("--output-dir", required=True, help="new directory below system temp or XDG_DATA_HOME/codex/artifacts")
    args = parser.parse_args(argv)
    try:
        report = verify(args)
    except (OSError, ValueError, VerificationError, subprocess.TimeoutExpired) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": report["ok"], "report": str(Path(report["output"]) / "report.json"), "wheel": report["wheel"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
