"""Server-owned Project reader selection; no browser-supplied executables/profiles."""

import re
from pathlib import PurePosixPath

from .project_read_client import ProjectReadError, canonical_host


def validate(value):
    if not isinstance(value, dict) or set(value) != {
        "enabled",
        "executable",
        "sha256",
        "profile",
        "host",
    }:
        raise ValueError("Project reader requires exact configuration fields")
    if type(value["enabled"]) is not bool:
        raise ValueError("Project reader enabled must be boolean")
    path = value["executable"]
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or str(PurePosixPath(path)) != path
        or ".." in PurePosixPath(path).parts
        or any(ord(c) < 32 for c in path)
    ):
        raise ValueError("Project reader executable must be a canonical absolute path")
    if not isinstance(value["sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["sha256"]
    ):
        raise ValueError("Project reader requires an accepted executable digest")
    if not isinstance(value["profile"], str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", value["profile"]
    ):
        raise ValueError("invalid Project profile")
    try:
        host = canonical_host(value["host"])
    except ProjectReadError:
        raise ValueError("invalid Project host") from None
    if host != value["host"]:
        raise ValueError("Project host must be canonical")
    return dict(value)


def selected(config):
    value = config.raw.get("project_integration", {}).get("reader")
    if value is None:
        return None
    result = validate(value)
    return result if result["enabled"] else None
