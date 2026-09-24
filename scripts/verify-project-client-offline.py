#!/usr/bin/env python3
"""Verify the shipped wrapper against an accepted native CLI without credentials.

Linux/bubblewrap acceptance, not a simulated pytest fixture. Requires an existing
wheel and accepted binary/hash; never downloads, logs in or queries real Bugs.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    wheel = args.wheel.resolve(strict=True)
    if args.output.exists():
        parser.error("refusing to overwrite acceptance evidence")
    with binary.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != args.sha256:
        parser.error("native binary differs from accepted digest")
    account_home = Path.home()
    if not account_home.is_absolute() or account_home == Path("/"):
        parser.error("a dedicated absolute account home is required")
    # Retain the account's HOME unchanged; mount an empty private directory there.
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "LANG", "PATH"}
    }
    results = []
    with tempfile.TemporaryDirectory(
        prefix="codex-project-native-acceptance-"
    ) as scratch:
        package = Path(scratch) / "package"
        with zipfile.ZipFile(wheel) as archive:
            if any(
                Path(name).is_absolute() or ".." in Path(name).parts
                for name in archive.namelist()
            ):
                parser.error("invalid wheel member path")
            archive.extractall(package)
        sandbox = [
            "/usr/bin/bwrap",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--tmpfs",
            str(account_home),
            "--tmpfs",
            "/opt",
            "--tmpfs",
            "/run",
            "--ro-bind",
            str(binary),
            "/opt/meegle-acceptance",
            "--ro-bind",
            str(package),
            "/opt/project-package",
            "--unshare-net",
            "--unshare-pid",
            "--proc",
            "/proc",
            "--die-with-parent",
            "--chdir",
            "/tmp",
        ]

        def invoke(argv):
            result = subprocess.run(
                sandbox + argv,
                capture_output=True,
                text=True,
                env=env,
                timeout=35,
                check=False,
            )
            if result.stderr:
                raise RuntimeError("native offline acceptance emitted stderr")
            return result

        version = invoke(["/opt/meegle-acceptance", "version"])
        if version.returncode != 0 or version.stdout.strip() != args.version:
            raise RuntimeError("native version check failed")
        launch = [
            "/usr/bin/python3",
            "-I",
            "-c",
            (
                'import sys; sys.path.insert(0,"/opt/project-package"); '
                "from k3_support.project_read_cli import main; raise SystemExit(main())"
            ),
            "--executable",
            "/opt/meegle-acceptance",
            "--sha256",
            actual,
            "--profile",
            "offline-acceptance",
            "--host",
            "project.feishu.cn",
        ]
        checks = [
            (
                ["auth-status"],
                1,
                {"state": "login_required", "host": None, "expires_in_minutes": None},
            ),
            (
                ["read-page", "--command", "user.me", "--params", "{}"],
                1,
                {"error": "auth_login_required"},
            ),
            (
                [
                    "decode",
                    "--url",
                    "https://project.feishu.cn/offline-fixture/issue/detail/123",
                ],
                0,
                {
                    "host": "project.feishu.cn",
                    "simple_name": "offline-fixture",
                    "work_item_type": "issue",
                    "work_item_id": "123",
                    "project_key_resolved": False,
                },
            ),
        ]
        for command, expected_exit, expected in checks:
            result = invoke(launch + command)
            if (
                result.returncode != expected_exit
                or json.loads(result.stdout) != expected
            ):
                raise RuntimeError("official CLI and read wrapper contract differ")
            results.append(
                {"action": command[0], "exit": result.returncode, "result": expected}
            )
    report = {
        "native_version": args.version,
        "native_sha256": actual,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "network_disabled": True,
        "account_home_hidden": True,
        "results": results,
        "live_schema_or_permissions_verified": False,
    }
    with args.output.open("x", encoding="utf-8") as destination:
        json.dump(report, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
