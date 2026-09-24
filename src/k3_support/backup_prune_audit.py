"""Durable intent before backup unlink; uncertain attempts never auto-retry."""

import json
import os
import re
import stat
from contextlib import contextmanager

from .ids import canonical_json, digest, new_id
from .timeutil import iso_now


def _read_receipt(parent_fd, receipt_id):
    if not isinstance(receipt_id, str) or not re.fullmatch("[0-9a-f]{64}", receipt_id):
        raise ValueError("invalid receipt identifier")
    fd = os.open(
        receipt_id + ".json",
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=parent_fd,
    )
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not a regular receipt")
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValueError("receipt too large")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("receipt_id") != receipt_id
        or value.get("state") not in {"prepared", "deleted"}
        or not isinstance(value.get("name"), str)
        or not re.fullmatch(r"support-[0-9]{8}T[0-9]{6}Z\.db", value["name"])
        or not isinstance(value.get("signature"), list)
        or len(value["signature"]) != 5
        or any(type(part) is not int for part in value["signature"])
        or digest({"name": value["name"], "signature": value["signature"]})
        != receipt_id
    ):
        raise ValueError("invalid receipt binding")
    return value


def inspect_receipt(config, *, receipt_id):
    """Observe the exact target, without changing intent or authorizing retries."""
    from .retention_recovery import _parent

    if not isinstance(receipt_id, str) or not re.fullmatch("[0-9a-f]{64}", receipt_id):
        raise ValueError("invalid receipt identifier")
    parent_fd, _ = _parent((config.data_dir / "backups" / "entry").absolute())
    try:
        journal = os.open(
            ".retention-audit",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            receipt = _read_receipt(journal, receipt_id)

            def target():
                try:
                    observed = os.stat(
                        receipt["name"], dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    return "missing", None
                signature = [
                    observed.st_dev,
                    observed.st_ino,
                    observed.st_size,
                    observed.st_mtime_ns,
                    observed.st_ctime_ns,
                ]
                if not stat.S_ISREG(observed.st_mode):
                    return "not_regular", signature
                return (
                    "same_version"
                    if signature == receipt["signature"]
                    else "different_version"
                ), signature

            first = target()
            if _read_receipt(journal, receipt_id) != receipt:
                raise ValueError("receipt changed during inspection; inspect again")
            second = target()
            state = second[0] if first == second else "changed_during_check"
            return {
                "receipt_id": receipt_id,
                "name": receipt["name"],
                "receipt_state": receipt["state"],
                "target_state": state,
                "observed_at": iso_now(),
                "read_only": True,
                "retry_authorized": False,
                "deletion_cause_verified": False,
                "note": "仅当前状态观察；文件缺失不证明删除原因。未修改记录或执行删除。",
            }
        finally:
            os.close(journal)
    finally:
        os.close(parent_fd)


def _write(fd, value):
    with os.fdopen(fd, "w") as stream:
        stream.write(canonical_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def deletion_receipt(parent_fd, *, name, observed, policy):
    directory = ".retention-audit"
    try:
        os.mkdir(directory, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    journal = os.open(
        directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
    )
    try:
        signature = [
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        ]
        identifier = digest({"name": name, "signature": signature})
        filename = identifier + ".json"
        record = {
            "schema_version": 1,
            "receipt_id": identifier,
            "name": name,
            "signature": signature,
            "policy": policy,
            "state": "prepared",
            "prepared_at": iso_now(),
            "deleted_at": None,
        }
        # Existing receipt, including an incomplete one, blocks retry for this
        # exact file version. Policy changes do not erase an uncertain attempt.
        descriptor = os.open(
            filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=journal,
        )
        _write(descriptor, record)
        os.fsync(journal)
        os.fsync(parent_fd)
        yield record
        os.fsync(parent_fd)
        completed = {**record, "state": "deleted", "deleted_at": iso_now()}
        temporary = "." + new_id("prune")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=journal,
            )
            _write(descriptor, completed)
            os.replace(temporary, filename, src_dir_fd=journal, dst_dir_fd=journal)
            os.fsync(journal)
        finally:
            try:
                os.unlink(temporary, dir_fd=journal)
            except FileNotFoundError:
                pass
    finally:
        os.close(journal)


def history(config, *, after_id="", limit=50):
    from .retention_recovery import _parent

    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("history limit must be 1..200")
    if not isinstance(after_id, str) or (
        after_id and not re.fullmatch("[0-9a-f]{64}", after_id)
    ):
        raise ValueError("invalid history cursor")
    path = config.data_dir / "backups" / ".retention-audit" / "entry"
    try:
        parent_fd, _ = _parent(path.absolute())
    except FileNotFoundError:
        return {"items": [], "next_cursor": None, "read_only": True}
    try:
        names = sorted(
            name
            for name in os.listdir(parent_fd)
            if re.fullmatch("[0-9a-f]{64}\\.json", name) and name[:-5] > after_id
        )
        items = []
        for name in names[:limit]:
            try:
                items.append(_read_receipt(parent_fd, name[:-5]))
            except (OSError, ValueError):
                items.append({"receipt_id": name[:-5], "state": "unreadable"})
        return {
            "items": items,
            "next_cursor": names[limit - 1][:-5] if len(names) > limit else None,
            "read_only": True,
            "note": "prepared/unreadable 表示未确认，不代表文件已删除。",
        }
    finally:
        os.close(parent_fd)
