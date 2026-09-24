"""Credential-free launch identity from a trusted directory descriptor.

The deployment must supply the directory FD, never a worker-selected directory.
This describes launch routing; it is not a provider billing attestation.
"""

import json
import os
import re
import stat
from dataclasses import dataclass
from urllib.parse import urlsplit

from .ids import digest

AGENTS = frozenset({"codex", "claude", "dsh", "opencode", "hermes"})


def validate_selection(value):
    """Public immutable identity only; no commands, credentials or permissions."""
    if (not isinstance(value, dict) or set(value) != {"agent", "contract_fingerprint"}
            or not isinstance(value["agent"], str) or value["agent"] not in AGENTS
            or not isinstance(value["contract_fingerprint"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["contract_fingerprint"])):
        raise ValueError("invalid coding execution selection")
    return dict(value)


@dataclass(frozen=True)
class ExecutionContract:
    provider: str
    base_url: str
    model: str
    reasoning: str
    wire_api: str
    fingerprint: str
    agent: str = "codex"

    def selection(self):
        return validate_selection({"agent": self.agent, "contract_fingerprint": self.fingerprint})


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate execution contract field")
        result[key] = value
    return result


def read_object_at(directory_fd, *, control_uid, worker_uid, name):
    if (type(control_uid) is not int or type(worker_uid) is not int
            or not 0 < control_uid < 4294967295 or not 0 < worker_uid < 4294967295
            or control_uid == worker_uid):
        raise ValueError("independent control and worker identities required")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.json", name):
        raise ValueError("contract basename required")
    directory = os.fstat(directory_fd)
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid not in {0, control_uid}
            or directory.st_mode & 0o022):
        raise ValueError("deployment-controlled contract directory required")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid not in {0, control_uid}
                or before.st_mode & 0o022 or before.st_nlink != 1):
            raise ValueError("deployment-controlled regular contract required")
        raw = stream.read(16385)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("contract changed while reading")
    if len(raw) > 16384:
        raise ValueError("execution contract too large")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
    return value


def load_at(directory_fd, *, control_uid, worker_uid, name="execution-contract.json"):
    value = read_object_at(directory_fd, control_uid=control_uid, worker_uid=worker_uid, name=name)
    fields = {"version", "provider", "base_url", "model", "reasoning", "wire_api"}
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise ValueError("unsupported execution contract")
    if value["version"] == 2:
        fields |= {"agent"}
        if (not isinstance(value.get("agent"), str) or value["agent"] not in AGENTS
                or not isinstance(value.get("model"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", value["model"])
                or not isinstance(value.get("reasoning"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value["reasoning"])
                or not isinstance(value.get("wire_api"), str)
                or value["wire_api"] not in {"responses", "messages", "chat_completions"}
                or (value["agent"] == "codex" and value["wire_api"] != "responses")
                or (value["agent"] == "claude" and value["wire_api"] != "messages")):
            raise ValueError("unsupported execution contract")
    elif (value["version"] != 1 or value.get("model") != "gpt-5.6-sol"
          or value.get("reasoning") != "medium" or value.get("wire_api") != "responses"):
        raise ValueError("unsupported execution contract")
    if (set(value) != fields
            or not isinstance(value["provider"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value["provider"])):
        raise ValueError("unsupported execution contract")
    url = value["base_url"]
    if not isinstance(url, str) or not 1 <= len(url) <= 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in url):
        raise ValueError("invalid provider URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or parsed.hostname.endswith(".invalid")):
        raise ValueError("credential-free HTTPS provider URL required")
    return ExecutionContract(**{key: value[key] for key in fields - {"version"}}, fingerprint=digest(value))
