import hashlib
import json
from pathlib import Path
import subprocess
import sys

from importlib.resources import files


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()


def source_repo(path, *, content="source"):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text(content)
    git(path, "add", "tracked.txt")
    git(path, "commit", "-qm", "source")
    return git(path, "rev-parse", "HEAD")


def run_payload(tmp_path, companions, *, command="printf '%s\\n%s\\n' \"$K3_VERIFICATION_SOURCES\" \"$PWD\""):
    primary_path = tmp_path / "source-primary"
    primary_commit = source_repo(primary_path)
    root = tmp_path / "job"
    root.mkdir()
    binding = {
        "job_id": "job-1", "request_id": "request-1", "repository": "primary",
        "source_path": str(primary_path), "base_commit": primary_commit,
        "root": str(root), "continuation": False, "companions": companions,
    }
    sampler = files("k3_support").joinpath("verification_source_probe.py").read_text().rsplit("\nif __name__", 1)[0]
    payload = files("k3_support").joinpath("investigation_checkout_payload.py").read_text()
    args = json.dumps({"binding": binding, "command": command})
    result = subprocess.run([sys.executable, "-I", "-S", "-c", sampler + "\n" + payload, args],
                            cwd=root, capture_output=True, text=True)
    return result, binding


def companion(tmp_path, name, *, content="source"):
    source = tmp_path / ("source-" + name)
    commit = source_repo(source, content=content)
    return {
        "repository": name, "source_path": str(source), "base_commit": commit,
        "relative_path": "repositories/" + hashlib.sha256(name.encode()).hexdigest()[:16],
    }


def test_composite_checkout_receipt_and_environment(tmp_path):
    extras = [companion(tmp_path, "kernel"), companion(tmp_path, "edk2")]
    result, binding = run_payload(tmp_path, extras)
    assert result.returncode == 0, result.stderr
    line, _, command_output = result.stdout.partition("\n")
    receipt = json.loads(line.removeprefix("K3_CHECKOUT_V1:"))
    assert receipt["source"]["path"] == str(Path(binding["root"]) / "repository")
    assert [item["repository"] for item in receipt["companions"]] == ["kernel", "edk2"]
    assert all(item["matched"] and item["tracked_content_matches"] for item in receipt["companions"])
    env_json, command_cwd = command_output.splitlines()
    paths = json.loads(env_json)
    assert paths == {
        "primary": str(Path(binding["root"]) / "repository"),
        "kernel": str(Path(binding["root"]) / extras[0]["relative_path"]),
        "edk2": str(Path(binding["root"]) / extras[1]["relative_path"]),
    }
    assert command_cwd == paths["primary"]
    assert (Path(paths["primary"]) / ".git/objects/info/alternates").exists() is False
    for name in ("kernel", "edk2"):
        assert (Path(paths[name]) / ".git/objects/info/alternates").exists() is False


def test_companion_failure_prevents_command_execution(tmp_path):
    extra = companion(tmp_path, "kernel")
    extra["seed_job_id"] = "seed-job"
    (Path(extra["source_path"]) / "tracked.txt").write_text("changed")
    marker = tmp_path / "command-ran"
    result, _ = run_payload(tmp_path, [extra], command=f"touch {marker}")
    assert result.returncode == 126
    assert "K3_CHECKOUT_V1:" not in result.stdout
    assert not marker.exists()


def test_companion_name_and_relative_path_are_validated_before_clone(tmp_path):
    extra = companion(tmp_path, "kernel")
    extra["relative_path"] = "repositories/other"
    result, binding = run_payload(tmp_path, [extra])
    assert result.returncode == 126
    assert not (Path(binding["root"]) / "repository").exists()
