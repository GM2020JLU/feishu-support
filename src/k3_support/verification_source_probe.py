"""Control-generated read-only source sampler, executable with python -I -S.

Checks raw tracked bytes against Git tree objects; ignores index cleanliness
claims, filters and assume-unchanged bits. This is a point-in-time sample, not
proof of continuous isolation, build provenance or a functional test oracle.
"""

import hashlib
import json
import os
import resource
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def _directory(start_fd, parts):
    fd = os.dup(start_fd)
    try:
        for part in parts:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def sample(binding):
    path = Path(binding["path"])
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid_source")
    start = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        root_fd = _directory(start, path.parts[1:])
    finally:
        os.close(start)
    try:
        return _sample(binding, root_fd)
    finally:
        os.close(root_fd)


def _sample(binding, root_fd):
    root = Path(binding["path"])
    if not root.is_absolute() or str(root.resolve(strict=True)) != str(root):
        raise ValueError("noncanonical_source")
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_NO_LAZY_FETCH": "1",
    }

    def git(*args):
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                [
                    "/usr/bin/git",
                    "--no-pager",
                    "-c",
                    "core.fsmonitor=false",
                    "-C",
                    f"/proc/self/fd/{root_fd}",
                    *args,
                ],
                env=env,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
                pass_fds=(root_fd,),
            )
            if result.returncode or output.tell() > 16 * 1024 * 1024:
                raise ValueError("git_probe_failed")
            output.seek(0)
            return output.read(16 * 1024 * 1024 + 1)

    if git("rev-parse", "--show-toplevel").decode().strip() != str(root):
        raise ValueError("not_repository_root")
    head = git("rev-parse", "HEAD").decode().strip()
    algorithm = git("rev-parse", "--show-object-format").decode().strip()
    if algorithm not in {"sha1", "sha256"}:
        raise ValueError("unsupported_object_format")
    base = binding["base_commit"]
    ancestor = git("merge-base", base, head).decode().strip() == base
    tree = git("ls-tree", "-r", "-z", "--full-tree", head)
    entries = tree.rstrip(b"\0").split(b"\0") if tree else []
    if len(entries) > 100000:
        raise ValueError("source_limit")
    total = 0
    matched = True
    for entry in entries:
        metadata, relative_raw = entry.split(b"\t", 1)
        mode, kind, expected = metadata.decode().split()
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise ValueError("unsupported_git_entry")
        relative = Path(os.fsdecode(relative_raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid_tree_path")
        # Walk using held directory descriptors, never following worker symlinks.
        parent_fd = _directory(root_fd, relative.parts[:-1])
        try:
            info = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
            if mode == "120000":
                if not stat.S_ISLNK(info.st_mode):
                    matched = False
                    continue
                data = os.fsencode(os.readlink(relative.name, dir_fd=parent_fd))
            else:
                if not stat.S_ISREG(info.st_mode) or bool(info.st_mode & 0o111) != (
                    mode == "100755"
                ):
                    matched = False
                    continue
                if info.st_size > 64 * 1024 * 1024:
                    raise ValueError("source_limit")
                fd = os.open(
                    relative.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent_fd,
                )
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or (
                        opened.st_dev,
                        opened.st_ino,
                    ) != (info.st_dev, info.st_ino):
                        raise ValueError("source_changed")
                    data = stream.read(64 * 1024 * 1024 + 1)
                    after = os.fstat(stream.fileno())
                    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise ValueError("source_changed")
        finally:
            os.close(parent_fd)
        total += len(data)
        if total > 2 * 1024 * 1024 * 1024 or len(data) > 64 * 1024 * 1024:
            raise ValueError("source_limit")
        actual = hashlib.new(
            algorithm, b"blob " + str(len(data)).encode() + b"\0" + data
        ).hexdigest()
        matched = matched and actual == expected
    end_head = git("rev-parse", "HEAD").decode().strip()
    # Untracked files, ignored build inputs, submodules and environment need their
    # own provenance policy. Do not infer their absence from this sampler.
    return {
        "repository": binding["repository"],
        "path": str(root),
        "head": head,
        "end_head": end_head,
        "base_is_ancestor": ancestor,
        "tracked_content_matches": matched,
        "tracked_count": len(entries),
        "matched": head == end_head == binding["candidate_commit"]
        and ancestor
        and matched,
        "coverage": "tracked_source_sample",
    }


def main():
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (
                min(16 * 1024 * 1024, hard)
                if hard != resource.RLIM_INFINITY
                else 16 * 1024 * 1024,
                hard,
            ),
        )
        bindings = json.loads(sys.argv[1])
        rows = [sample(binding) for binding in bindings]
        print(json.dumps({"sources": rows}, ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        # No paths, Git stderr, repository contents or ambient details in errors.
        print('{"error":"source_observation_unavailable"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
