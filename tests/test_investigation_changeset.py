"""Contract tests for the fixed, read-only investigation changeset probe."""

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _git(repo, *args):
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Changeset Test",
        GIT_AUTHOR_EMAIL="changeset-test@example.invalid",
        GIT_COMMITTER_NAME="Changeset Test",
        GIT_COMMITTER_EMAIL="changeset-test@example.invalid",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_TERMINAL_PROMPT="0",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    ).stdout.strip()


def _repo(path):
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Changeset Test")
    _git(path, "config", "user.email", "changeset-test@example.invalid")
    (path / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
    (path / "source.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return _git(path, "rev-parse", "HEAD")


def _run_probe(repo, base_commit):
    source_path = ROOT / "src/k3_support/verification_source_probe.py"
    changeset_path = ROOT / "src/k3_support/investigation_changeset_probe.py"
    source = source_path.read_text(encoding="utf-8").rsplit("\nif __name__", 1)[0]
    changeset = changeset_path.read_text(encoding="utf-8").rsplit("\nif __name__", 1)[0]
    program = source + "\n" + changeset + '\nif __name__ == "__main__":\n    raise SystemExit(changeset_main())\n'
    binding = {
        "repository": "test-repository",
        "path": str(repo),
        "base_commit": base_commit,
        "candidate_commit": base_commit,
    }
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, json.dumps([binding])],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_changeset_covers_every_commit_binary_patch_and_untracked_files(tmp_path):
    repo = tmp_path / "repo"
    base = _repo(repo)

    (repo / "source.txt").write_text("base\nfirst change\n", encoding="utf-8")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "-qm", "first change")
    intermediate = _git(repo, "rev-parse", "HEAD")

    (repo / "source.txt").write_text("base\nfirst change\nsecond change\n", encoding="utf-8")
    (repo / "image.bin").write_bytes(b"\x00\x01\xffbinary\x00payload")
    _git(repo, "add", "source.txt", "image.bin")
    _git(repo, "commit", "-qm", "second change")
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "scratch.ignored").write_text("ignored but disclosed\n", encoding="utf-8")
    (repo / "loose.txt").write_text("untracked and disclosed\n", encoding="utf-8")

    result = _run_probe(repo, base)
    changeset = result["changeset"]

    assert changeset["state"] == "observed"
    assert changeset["coverage"] == "complete_committed_changeset_v1"
    assert changeset["base_commit"] == base
    assert changeset["head_commit"] == head
    assert changeset["commits"] == [intermediate, head]

    patch = base64.b64decode(changeset["patch_b64"], validate=True)
    assert b"GIT binary patch" in patch
    assert hashlib.sha256(patch).hexdigest() == changeset["patch_sha256"]
    paths = [path for path in base64.b64decode(changeset["paths_b64"], validate=True).split(b"\0") if path]
    assert set(paths) == {b"image.bin", b"source.txt"}

    untracked = [path for path in base64.b64decode(changeset["untracked_paths_b64"], validate=True).split(b"\0") if path]
    assert set(untracked) == {b"loose.txt", b"scratch.ignored"}


def test_changeset_reports_tracked_worktree_edits_separately(tmp_path):
    repo = tmp_path / "repo"
    base = _repo(repo)
    (repo / "source.txt").write_text("base\ncommitted\n", encoding="utf-8")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "-qm", "committed change")
    (repo / "source.txt").write_text("base\ncommitted\nlocal edit\n", encoding="utf-8")

    result = _run_probe(repo, base)

    assert result["changeset"]["state"] == "observed"
    assert result["changeset"]["tracked_content_matches"] is False
    assert result["sources"][0]["tracked_content_matches"] is False


def test_non_ancestor_base_makes_changeset_unavailable(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    _git(repo, "checkout", "-qb", "side")
    (repo / "side.txt").write_text("side branch\n", encoding="utf-8")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-qm", "side commit")
    side_base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "--detach", "HEAD~1")
    (repo / "main.txt").write_text("main history\n", encoding="utf-8")
    _git(repo, "add", "main.txt")
    _git(repo, "commit", "-qm", "main commit")

    result = _run_probe(repo, side_base)

    assert result["sources"][0]["base_is_ancestor"] is False
    assert result["changeset"] == {"state": "unavailable"}
