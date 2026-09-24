import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "baseline_audit", Path(__file__).parents[1] / "scripts/audit-source-baseline.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    source = root / "src/k3_support"
    source.mkdir(parents=True)
    (source / "same.py").write_text("same")
    (source / "change.py").write_text("old")
    (source / "deleted.py").write_text("old")
    (root / ".gitignore").write_text("secret.py\n__pycache__/\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "fixture"],
                   check=True, capture_output=True)
    return root, source


def test_compare_tracks_deletions_untracked_and_excludes_ignored(tmp_path):
    root, source = repository(tmp_path)
    installed = tmp_path / "installed"
    installed.mkdir()
    for name in ("same.py", "change.py", "deleted.py"):
        (installed / name).write_bytes((source / name).read_bytes())
    (source / "change.py").write_text("new")
    (source / "deleted.py").unlink()
    (source / "new.py").write_text("new")
    (source / "secret.py").write_text("must not appear")
    before = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"])
    result = audit.compare(root, installed)
    assert result["counts"] == {"same": 1, "modified": 1, "source_only": 1, "installed_only": 1}
    assert "secret.py" not in str(result)
    assert before == subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"])


def test_symlink_and_path_escape_are_rejected(tmp_path):
    root, source = repository(tmp_path)
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "file.py").write_text("fixture")
    with pytest.raises(ValueError):
        audit.compare(root, installed, "../outside")
    (source / "external.py").symlink_to(installed / "file.py")
    with pytest.raises(ValueError, match="symlink"):
        audit.compare(root, installed)
