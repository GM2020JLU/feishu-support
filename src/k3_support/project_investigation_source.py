"""Immutable source expectations and deployment fences, not checkout evidence."""

import re

from .execution_transport import target
from .ids import digest

FIELDS = {"branch", "base_commit", "node", "version", "deployment_fingerprint"}


def validate(source):
    if not isinstance(source, dict) or set(source) not in (FIELDS, FIELDS | {"candidate_request_id"}):
        raise ValueError("investigation requires exact source binding")
    if any(not isinstance(value, str) for value in source.values()):
        raise ValueError("invalid source binding value")
    if "candidate_request_id" in source and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source["candidate_request_id"]):
        raise ValueError("invalid candidate receipt identity")
    branch = source["branch"]
    if (not 1 <= len(branch) <= 256 or branch.startswith(('-', '/', '.'))
            or branch.endswith(('/', '.')) or branch == '@'
            or any(part.startswith('.') or part.endswith('.lock') or not part for part in branch.split('/'))
            or any(text in branch for text in ('..', '@{'))
            or re.search(r'[\x00-\x20\x7f~^:?*\[\\]', branch)):
        raise ValueError("invalid source branch")
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', source["base_commit"]):
        raise ValueError("source baseline requires a full lowercase commit hash")
    if not re.fullmatch(r'[0-9a-f]{64}', source["deployment_fingerprint"]):
        raise ValueError("invalid source deployment fingerprint")
    if (not 1 <= len(source["node"]) <= 256 or len(source["version"]) > 256
            or any(ord(c) < 32 or ord(c) == 127 for c in source["node"] + source["version"])):
        raise ValueError("invalid source node or version")
    return source


def selection(config, repository):
    if repository not in config.raw['repositories']:
        raise ValueError("source repository unavailable")
    runtime = target(config)
    return {
        'node': runtime['host'],
        'deployment_fingerprint': digest({
            'runtime': runtime, 'repository': repository,
            'path': config.raw['repositories'][repository]['path'],
            'work_root': config.runtime('remote_worktree_root'),
        }),
    }


def require_current(config, repository, source):
    validate(source)
    configurations = [config]
    if config.source_config_guard is not None:
        configurations.append(config.source_config_guard())
    for current in configurations:
        expected = selection(current, repository)
        if any(source[key] != value for key, value in expected.items()):
            raise ValueError("investigation source deployment changed; prepare a new task")


def monitor_source_config(config):
    """Private services re-read their own source policy; never follow a new DB."""
    from dataclasses import replace

    from .broker_storage import check_database
    from .config import load_config

    database = config.database_path.absolute()
    identity = check_database(database)

    def guard():
        try:
            current = load_config(config.path)
            if (current.database_path.absolute() != database
                    or check_database(database) != identity):
                raise ValueError("investigation control database changed")
            return current
        except OSError:
            raise ValueError("investigation source configuration unavailable") from None

    return replace(config, source_config_guard=guard)
