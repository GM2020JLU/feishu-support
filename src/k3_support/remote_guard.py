"""Wrap a control-generated sandbox command with a bounded remote lease watcher."""

import shlex
from importlib.resources import files
from pathlib import PurePosixPath


def receipt_directory(runtime):
    value = runtime.get("remote_receipt_directory")
    if value is None:
        return None
    if (not isinstance(value, str) or not value.startswith("/") or "\x00" in value
            or len(value) > 4096 or str(PurePosixPath(value)) != value
            or ".." in PurePosixPath(value).parts):
        raise ValueError("canonical remote receipt directory required")
    path = PurePosixPath(value)
    exposed = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/proc", "/sys", "/dev", "/tmp"]
    exposed += [runtime.get(k) for k in ("remote_workspace_root", "remote_source_root", "remote_worktree_root")]
    exposed += runtime.get("remote_toolchain_roots", [])
    for item in exposed:
        if item and (path.is_relative_to(PurePosixPath(item)) or PurePosixPath(item).is_relative_to(path)):
            raise ValueError("remote receipt directory overlaps sandbox exposure")
    return value


def wrap(command, *, receipt_directory=None, request_id=None):
    if not isinstance(command, str) or not command or len(command.encode()) > 100000:
        raise ValueError("bounded sandbox command required")
    source = files("k3_support").joinpath("remote_guard_payload.py").read_text()
    extra = []
    if receipt_directory is not None or request_id is not None:
        from uuid import UUID
        if (not isinstance(receipt_directory, str) or not receipt_directory.startswith("/")
                or "\x00" in receipt_directory or len(receipt_directory) > 4096
                or not isinstance(request_id, str) or str(UUID(request_id)) != request_id):
            raise ValueError("absolute receipt directory and canonical request required")
        source = files("k3_support").joinpath("remote_journal.py").read_text() + "\n" + source
        extra = [receipt_directory, request_id]
    return shlex.join(["/usr/bin/python3", "-I", "-S", "-c", source, command, *extra])
