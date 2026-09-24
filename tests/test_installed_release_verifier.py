from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify-installed-release.py"
SPEC = importlib.util.spec_from_file_location("installed_release_verifier", SCRIPT)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


@pytest.fixture(autouse=True)
def verifier_temporary_root(tmp_path, monkeypatch):
    # pytest --basetemp may live outside Python's default /tmp. Declare the
    # same private temporary root to the verifier and its actual subprocesses;
    # do not relax reserve_output's production path policy.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(verifier.tempfile, "tempdir", str(tmp_path))


@pytest.mark.parametrize('outcome', ['success', 'failure', 'timeout', 'missing'])
def test_step_elapsed_time_is_recorded_for_all_outcomes(tmp_path, monkeypatch, outcome):
    ticks = iter([100.0, 101.25])
    monkeypatch.setattr(verifier.time, 'monotonic', lambda: next(ticks))
    def run(*args, **kwargs):
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired('fixture', 180)
        if outcome == 'missing':
            raise FileNotFoundError('fixture')
        return subprocess.CompletedProcess('fixture', int(outcome == 'failure'), '{}', '')
    monkeypatch.setattr(verifier.subprocess, 'run', run)
    runner = verifier.Runner(tmp_path, {})
    if outcome == 'success':
        assert runner.run('restore', ['fixture'], as_json=True) == {}
    else:
        with pytest.raises((verifier.VerificationError, subprocess.TimeoutExpired, FileNotFoundError)):
            runner.run('restore', ['fixture'])
    assert len(runner.steps) == 1
    assert runner.steps[0]['elapsed_seconds'] == 1.25
    assert runner.steps[0]['name'] == 'restore'


def test_clean_cache_requires_explicit_wheelhouse(tmp_path):
    with pytest.raises(verifier.VerificationError, match="requires an explicit"):
        from argparse import Namespace
        verifier.verify(Namespace(source=str(SCRIPT.parent.parent), wheelhouse=None,
                                  clean_cache=True, wheel=None,
                                  output_dir=str(tmp_path / "unused")))
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize('count', [0, -1, 10001, True, '1000'])
def test_capacity_fixture_rejects_invalid_count_before_creating_files(count, tmp_path):
    from argparse import Namespace
    with pytest.raises(verifier.VerificationError, match='fixture cases'):
        verifier.verify(Namespace(fixture_cases=count, output_dir=str(tmp_path / 'unused')))
    assert not (tmp_path / 'unused').exists()


def test_wheelhouse_inputs_reject_symlinks_empty_and_non_wheels(tmp_path):
    with pytest.raises(verifier.VerificationError, match="empty"):
        verifier.wheelhouse_manifest(tmp_path)
    wheel = tmp_path / "fixture.whl"
    wheel.write_bytes(b"fixture")
    assert verifier.wheelhouse_manifest(tmp_path) == {"fixture.whl": verifier.sha256(b"fixture")}
    (tmp_path / "escape.whl").symlink_to(wheel)
    with pytest.raises(verifier.VerificationError, match="regular"):
        verifier.wheelhouse_manifest(tmp_path)


def test_locked_constraints_reject_ambiguous_versions(tmp_path):
    (tmp_path / "uv.lock").write_text('''[[package]]
name="example"
version="1"
source={registry="https://pypi.org/simple"}
''')
    assert verifier.locked_constraints(tmp_path) == "example==1\n"
    with (tmp_path / "uv.lock").open("a") as stream:
        stream.write('''[[package]]
name="example"
version="2"
source={registry="https://pypi.org/simple"}
''')
    with pytest.raises(verifier.VerificationError, match="multiple locked"):
        verifier.locked_constraints(tmp_path)


def test_dependency_hash_gate_rejects_unlocked_wheels(tmp_path):
    (tmp_path / "uv.lock").write_text('package=[]\n')
    (tmp_path / "config").mkdir()
    (tmp_path / "config/build-requirements.lock").write_text(
        "--hash=sha256:" + "a" * 64)
    verifier.verify_dependency_hashes(tmp_path, {"fixture.whl": "a" * 64})
    with pytest.raises(verifier.VerificationError, match="not covered"):
        verifier.verify_dependency_hashes(tmp_path, {"fixture.whl": "b" * 64})


def test_output_is_new_private_and_rejects_existing_source_and_symlinks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    output = verifier.reserve_output(tmp_path / "new-smoke", source)
    assert output.stat().st_mode & 0o777 == 0o700
    with pytest.raises(verifier.VerificationError, match="already exists"):
        verifier.reserve_output(output, source)
    with pytest.raises(verifier.VerificationError, match="outside the source"):
        verifier.reserve_output(source / "new", source)
    link = tmp_path / "link"
    link.symlink_to(output, target_is_directory=True)
    with pytest.raises(verifier.VerificationError, match="symlink"):
        verifier.reserve_output(link / "child", source)
    with pytest.raises(verifier.VerificationError, match="below the temporary"):
        verifier.reserve_output(Path.home() / "must-not-create-live-test-dir", source)


def test_environment_drops_credentials_and_import_paths(tmp_path, monkeypatch):
    for name in ("PYTHONPATH", "PYTHONHOME", "TELEGRAM_BOT_TOKEN", "LARK_APP_SECRET", "OPENAI_API_KEY", "SSH_AUTH_SOCK", "HTTP_PROXY"):
        monkeypatch.setenv(name, "must-not-inherit")
    value = verifier.private_environment(tmp_path, path="fixture-path")
    assert "must-not-inherit" not in value.values()
    assert value["UV_OFFLINE"] == "true" and value["UV_PYTHON_DOWNLOADS"] == "never"
    assert value["HERMES_HOME"].startswith(str(tmp_path))


def test_wheel_parity_detects_missing_or_changed_resources(tmp_path):
    wheel = tmp_path / "fixture.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("k3_support/__init__.py", "fixture")
    expected = {"k3_support/__init__.py": verifier.sha256(b"fixture")}
    assert verifier.verify_wheel(wheel, expected) == expected
    for mutation in (
        {**expected, "k3_support/migrations/999_required.sql": "required"},
        {"k3_support/__init__.py": "different"},
    ):
        with pytest.raises(verifier.VerificationError, match="parity failed"):
            verifier.verify_wheel(wheel, mutation)


def test_wheel_rejects_path_traversal(tmp_path):
    wheel = tmp_path / "unsafe.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("../escape", "bad")
    with pytest.raises(verifier.VerificationError, match="unsafe members"):
        verifier.verify_wheel(wheel, {})


def test_dependency_failure_is_nonzero_with_private_evidence(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "src/k3_support").mkdir(parents=True)
    (source / "src/k3_support/__init__.py").write_text("")
    (source / "pyproject.toml").write_text('[project]\nname="k3-support"\nversion="0.1.0"\n[project.scripts]\n')
    monkeypatch.setattr(verifier.shutil, "which", lambda _: "/fixture/uv")
    commands = []

    def failed(command, **kwargs):
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1, "", "offline cache miss")

    monkeypatch.setattr(verifier.subprocess, "run", failed)
    output = tmp_path / "failed-install"
    assert verifier.main(["--source", str(source), "--output-dir", str(output)]) == 1
    report = json.loads((output / "report.json").read_text())
    assert report["ok"] is False and report["deployment_performed"] is False
    assert "pre-cached" in report["error"]["message"]
    assert "--offline" in commands[0][0] and commands[0][1]["cwd"] == output
    assert (output / "report.json").stat().st_mode & 0o777 == 0o600


def test_source_symlinks_are_rejected(tmp_path):
    package = tmp_path / "src/k3_support"
    package.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("")
    (package / "escape.py").symlink_to(SCRIPT)
    with pytest.raises(verifier.VerificationError, match="source symlinks"):
        verifier.source_manifest(tmp_path)


def test_guard_denies_real_commands_and_network_without_attempting_them(tmp_path):
    # Execute the installed guard in a disposable interpreter, never in pytest.
    probe = verifier.GUARD + """
import socket, subprocess
blocked = []
for name, action in (("transport", lambda: subprocess.run(["/usr/bin/hermes", "send"])),
                     ("network", lambda: socket.create_connection(("example.com", 443)))):
    try:
        action()
    except PermissionError as error:
        assert "offline installed smoke forbids" in str(error)
        blocked.append(name)
assert blocked == ["transport", "network"]
"""
    result = subprocess.run([sys.executable, "-I", "-c", probe], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr


def test_real_offline_wheel_install_and_isolated_runtime(tmp_path):
    """Fail, do not skip/fetch, if release dependencies were not pre-cached."""
    if shutil.which("uv") is None:
        pytest.skip("uv is a prerequisite for the optional installed-wheel acceptance tool")
    output = tmp_path / "installed-release"
    # A poisoned path must not satisfy installed imports. The child allowlist
    # and -I together ensure this cannot silently turn into a source-tree test.
    poison = tmp_path / "poison/k3_support"
    poison.mkdir(parents=True)
    (poison / "__init__.py").write_text("raise RuntimeError('source import contamination')\n")
    env = {**os.environ, "PYTHONPATH": str(poison.parent), "OPENAI_API_KEY": "synthetic-secret-not-to-inherit"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--fixture-cases", "12", "--output-dir", str(output)], cwd=tmp_path,
        env=env, capture_output=True, text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["ok"] and report["read_commands_preserved_state"]
    assert report['fixture_cases'] == 12
    assert report['restore']['probe']['counts']['cases'] == 12
    assert report['workflow_restore'] == {'source_unchanged': True, 'attachment_restored': True,
                                          'authority_fenced': True, 'activation_allowed': False}
    assert report["installed"]["guard_verified"] and report["installed"]["previous_schema_upgrade"]
    assert not report["deployment_performed"] and not report["external_auth_verified"]
    assert set(report["runtime_doctor"]["commands"]) == {"hermes", "semantic", "lark_cli"}
    assert all(Path(path).is_relative_to(output / "venv") for path in report["installed"]["origins"].values())
    assert report["restore"]["logical_digest"] == report["installed"]["logical_digest"]
    for path in output.glob("*.log"):
        assert "synthetic-secret-not-to-inherit" not in path.read_text()
    # A release verifier may consume a prebuilt/frozen wheel; it must still
    # verify source/resource parity and may never silently rebuild that wheel.
    reused = tmp_path / "existing-wheel-release"
    repeat = subprocess.run(
        [sys.executable, str(SCRIPT), "--wheel", report["wheel"]["path"], "--output-dir", str(reused)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180, check=False,
    )
    assert repeat.returncode == 0, repeat.stderr
    reused_report = json.loads((reused / "report.json").read_text())
    assert reused_report["ok"] and reused_report["wheel"]["sha256"] == report["wheel"]["sha256"]
    assert "build" not in {step["name"] for step in reused_report["steps"]}
