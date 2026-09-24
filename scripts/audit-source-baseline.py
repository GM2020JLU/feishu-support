#!/usr/bin/env python3
"""Read-only comparison of a Git-visible source package and an installed package.

No imports from the inspected package and no executable or configuration loading.
Output contains relative paths and hashes, never file contents. Installation
metadata and runtime data are deliberately outside this package comparison.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def file_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"regular source file required: {path.name}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installed_inventory(package: Path) -> dict[str, str]:
    if not package.is_dir() or package.is_symlink():
        raise ValueError("installed package must be a regular directory")
    result = {}
    for path in sorted(package.rglob("*")):
        if "__pycache__" in path.relative_to(package).parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("installed package contains a symlink")
        if path.is_file():
            result[path.relative_to(package).as_posix()] = file_hash(path)
    if not result:
        raise ValueError("installed package is empty")
    return result


def compare(repository: Path, installed: Path, package: str = "src/k3_support") -> dict:
    repository = repository.resolve(strict=True)
    relative = Path(package)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise ValueError("package must be a repository-relative directory")
    source = repository / relative
    if source.is_symlink() or not source.resolve().is_relative_to(repository):
        raise ValueError("source package must remain inside repository")
    command = ["git", "-C", str(repository)]
    names = subprocess.check_output(
        command + ["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", package]
    ).decode().split("\0")
    current = {}
    for name in sorted(set(filter(None, names))):
        path = repository / name
        if path.is_symlink():
            raise ValueError("source package contains a symlink")
        if not path.exists():  # tracked deletion
            continue
        if not path.resolve().is_relative_to(source.resolve()):
            raise ValueError("source file escapes package")
        current[path.relative_to(source).as_posix()] = file_hash(path)
    if not current:
        raise ValueError("no Git-visible package files")
    deployed = installed_inventory(installed)
    groups = {key: [] for key in ("same", "modified", "source_only", "installed_only")}
    for name in sorted(current.keys() | deployed.keys()):
        left, right = deployed.get(name), current.get(name)
        group = ("source_only" if left is None else "installed_only" if right is None
                 else "same" if left == right else "modified")
        groups[group].append({"path": name, "installed_sha256": left, "source_sha256": right})
    return {
        "version": 1,
        "scope": "Git-visible package files only; not release readiness or installed-wheel verification",
        "git_head": subprocess.check_output(command + ["rev-parse", "HEAD"], text=True).strip(),
        "package": package,
        "counts": {key: len(value) for key, value in groups.items()},
        "files": groups,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--installed-package", type=Path, required=True)
    parser.add_argument("--package", default="src/k3_support")
    args = parser.parse_args()
    print(json.dumps(compare(args.repository, args.installed_package, args.package),
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
