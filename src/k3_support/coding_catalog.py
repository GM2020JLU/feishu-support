"""Deployment-owned coding choices; clients select IDs, never launch settings."""

import os
import re
from pathlib import Path

from .broker_execution_contract import load_at


def validate(value):
    if not isinstance(value, dict) or len(value) > 16:
        raise ValueError("coding_executors must be a mapping with at most 16 entries")
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", key):
            raise ValueError("invalid coding executor ID")
        if not isinstance(item, dict) or set(item) - {"contract_name"} != {
            "label",
            "contract_directory",
            "worker_uid",
        }:
            raise ValueError(
                "coding executor fields must be label, contract_directory and worker_uid"
            )
        if "contract_name" in item and (not isinstance(item["contract_name"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+\.json", item["contract_name"])):
            raise ValueError("contract basename required")
        label, directory, uid = (
            item["label"],
            item["contract_directory"],
            item["worker_uid"],
        )
        if (
            not isinstance(label, str)
            or not 1 <= len(label) <= 80
            or label != label.strip()
            or any(ord(c) < 32 for c in label)
        ):
            raise ValueError("invalid coding executor label")
        if (
            not isinstance(directory, str)
            or not 1 <= len(directory) <= 4096
            or any(ord(c) < 32 for c in directory)
            or not Path(directory).is_absolute()
            or ".." in Path(directory).parts
            or str(Path(directory)) != directory
        ):
            raise ValueError("absolute normalized coding contract directory required")
        if type(uid) is not int or not 0 < uid < 4294967295:
            raise ValueError("independent coding worker UID required")
    return value


def _contract(config, executor_id):
    entries = config.raw.get("coding_executors", {})
    if not isinstance(executor_id, str) or executor_id not in entries:
        raise ValueError("unknown configured coding executor")
    item = entries[executor_id]
    fd = os.open(
        item["contract_directory"],
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        return load_at(fd, control_uid=os.geteuid(), worker_uid=item["worker_uid"],
                       name=item.get("contract_name", "execution-contract.json"))
    finally:
        os.close(fd)


def choices(config):
    result = []
    for executor_id, item in config.raw.get("coding_executors", {}).items():
        entry = {"id": executor_id, "label": item["label"], "status": "unavailable"}
        try:
            contract = _contract(config, executor_id)
            entry.update(
                status="configured",
                agent=contract.agent,
                model=contract.model,
                reasoning=contract.reasoning,
                contract_fingerprint=contract.fingerprint,
            )
        except (OSError, ValueError):
            entry["reason"] = "deployment_contract_unavailable"
        result.append(entry)
    return {"items": result, "worker_health": "not_checked", "read_only": True}


def resolve(config, executor_id, *, expected_fingerprint):
    contract = _contract(config, executor_id)
    if contract.fingerprint != expected_fingerprint:
        raise ValueError("coding deployment changed; refresh choices")
    return contract
