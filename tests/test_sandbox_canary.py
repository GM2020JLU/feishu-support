from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "virtual_root", ["/srv/synthetic-project", "/home/synthetic-user/project"]
)
def test_real_bubblewrap_with_only_synthetic_project_resources(tmp_path, virtual_root):
    fixture = tmp_path / "sandbox-canary"
    fixture.mkdir()
    (fixture / "SYNTHETIC_ONLY").write_text("no production resources\n")
    empty_home = fixture / "launcher-home"
    empty_home.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).with_name("sandbox_canary.py")),
            str(fixture),
            virtual_root,
        ],
        cwd=fixture,
        env={"PATH": "/usr/bin:/bin", "HOME": str(empty_home)},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    if report["status"] == "unavailable":
        pytest.skip(report["reason"])
    assert report["status"] == "passed", report
    assert "shared_repo_objects" in report["checks"]
    assert "namespace_descendants_exit_including_setsid" in report["checks"]
