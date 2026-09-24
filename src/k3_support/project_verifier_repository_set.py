"""Shared bindings for a verifier step that reads several configured sources."""

import hashlib
from pathlib import PurePosixPath

from .project_investigation_source import validate


def source_map(value, primary_source, primary_repository):
    """Return the exact repository-name to source map for a verifier selection.

    ``sources`` is optional for legacy single-repository selections. When
    present it is the complete set, including the primary repository.
    """
    validate(primary_source)
    if (not isinstance(primary_repository, str)
            or not 1 <= len(primary_repository) <= 128 or any(ord(c) < 32 for c in primary_repository)):
        raise ValueError("primary verification repository unavailable")
    extras = value.get("sources")
    if extras is None:
        return {primary_repository: primary_source}
    if not isinstance(extras, dict) or not extras:
        raise ValueError("verification sources must be a non-empty repository map")
    result = {}
    for name, source in extras.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or any(ord(c) < 32 for c in name):
            raise ValueError("invalid configured verification repository")
        validate(source)
        result[name] = source
    if result.get(primary_repository) != primary_source:
        raise ValueError("primary investigation source differs from verification source map")
    return result


def workspace_paths(config, case_id, job_id, repositories):
    """Map configured names to stable, per-job source paths.

    The first repository owns the historical ``repository`` path. Extra
    repositories use a short hash of the configured name to avoid path
    collisions and unsafe names.
    """
    if (not isinstance(repositories, (list, tuple)) or not repositories
            or any(not isinstance(name, str) or name not in config.raw["repositories"]
                   for name in repositories)
            or len(repositories) != len(set(repositories))):
        raise ValueError("workspace repositories must be configured and distinct")
    root = (PurePosixPath(config.runtime("remote_worktree_root")) / case_id
            / ("investigation-" + job_id))
    return {
        name: str(root / "repository") if index == 0 else
        str(root / "repositories" / hashlib.sha256(name.encode()).hexdigest()[:16])
        for index, name in enumerate(repositories)
    }
