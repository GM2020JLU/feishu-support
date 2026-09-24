"""Source-bound attachment downloads and private verified artifact custody."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from .bounded_cli import OutputLimitError, run
from .ids import digest
from .project_attachment_transfer import MAX_BYTES, validate
from .project_read_client import ProjectReadError
from .project_read_snapshot import SnapshotReader
from .timeutil import iso_now


def selection(conn, actor, source):
    from .project_activity import _get

    if not isinstance(source, dict) or set(source) != {
        "activity_id",
        "field_key",
        "source_digest",
    }:
        raise ValueError("exact attachment selection required")
    row = _get(conn, actor, source["activity_id"])
    if row["state"] != "succeeded" or row["kind"] != "attachments":
        raise ValueError("accepted attachment inventory required")
    matches = [
        i
        for i in json.loads(row["result_json"])["items"]
        if i["state"] == "listed"
        and i["field_key"] == source["field_key"]
        and i["source_digest"] == source["source_digest"]
    ]
    if len(matches) != 1 or not matches[0]["has_source_reference"]:
        raise ValueError("observed attachment reference required")
    return row["bug_id"], matches[0]


def enqueue(
    conn, config, *, actor, activity_id, field_key, source_digest, grant_id, request_id
):
    from .project_activity import enqueue as queue

    source = {
        "activity_id": activity_id,
        "field_key": field_key,
        "source_digest": source_digest,
    }
    bug_id, _ = selection(conn, actor, source)
    return queue(
        conn,
        config,
        actor=actor,
        bug_id=bug_id,
        grant_id=grant_id,
        request_id=request_id,
        kind="attachment_download",
        source=source,
    )


def storage(config):
    parent = config.data_dir.absolute()
    info = parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise ValueError("protected attachment parent required")
    root = parent / "project-attachments"
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("private attachment storage required")
    return root


def _member(client, destination, source, guard):
    observed = SnapshotReader(client, before_read=guard).collect(destination)
    definition = observed["read_evidence"]["attachment_fields"].get(source["field_key"])
    values = observed["snapshot"]["fields"].get(source["field_key"])
    if (
        not definition
        or definition["type"] != "multi-file"
        or not isinstance(values, list)
    ):
        raise ProjectReadError("attachment_source_changed")
    matches = [
        v
        for v in values
        if isinstance(v, dict) and digest(v) == source["source_digest"]
    ]
    if (
        len(matches) != 1
        or not isinstance(matches[0].get("url"), str)
        or not matches[0]["url"]
    ):
        raise ProjectReadError("attachment_source_changed")
    return matches[0]


def collect(client, config, destination, source, *, guard, runner=run):
    member = _member(client, destination, source, guard)
    guard()
    plan = client.prepare_attachment_download(destination, member["url"])
    validate(plan)
    root = storage(config)
    # Serialize disk reservations across service instances. A crashed transfer
    # leaves an unreferenced private staging directory; it is never served.
    lock_fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    stage = None
    accepted = False
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        entries = list(root.iterdir())
        if len(entries) > 512 or shutil.disk_usage(root).free < MAX_BYTES * 2:
            raise ProjectReadError("attachment_storage_full")
        used = 0
        for entry in entries:
            if entry.name == ".lock":
                continue
            if not entry.is_dir() or entry.is_symlink():
                raise ValueError("unrecognized attachment storage entry")
            for file in entry.iterdir():
                info = file.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("unrecognized attachment storage entry")
                used += info.st_size
        if used + MAX_BYTES > 2 * 1024 * 1024 * 1024:
            raise ProjectReadError("attachment_storage_full")
        stage = Path(tempfile.mkdtemp(prefix="file-", dir=root))
        plan_path = stage / "plan.json"
        with plan_path.open("x", encoding="utf-8") as output:
            os.chmod(plan_path, 0o600)
            json.dump(plan, output)
        guard()
        try:
            result = runner(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("project_attachment_transfer.py")),
                    str(plan_path),
                    str(stage / "content"),
                ],
                env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                timeout=60,
                stdout_limit=4096,
                stderr_limit=4096,
            )
        finally:
            plan_path.unlink(missing_ok=True)
        guard()
        if result.returncode != 0:
            raise ProjectReadError("attachment_transfer_failed")
        receipt = json.loads(result.stdout)
        if (
            not isinstance(receipt, dict)
            or type(receipt.get("size_bytes")) is not int
            or not 0 <= receipt["size_bytes"] <= MAX_BYTES
            or not isinstance(receipt.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"])
            or receipt.get("server_signature_checked") is not True
            or type(receipt.get("parts")) is not int
            or not 1 <= receipt["parts"] <= 64
        ):
            raise ProjectReadError("invalid_attachment_receipt")
        with verified_file(stage / "content", receipt):
            pass
        _member(client, destination, source, guard)
        guard()
        accepted = True
        public = {
            k: receipt[k]
            for k in ("size_bytes", "sha256", "parts", "server_signature_checked")
        }
        return {
            "items": [
                public
                | {
                    "artifact_id": stage.name,
                    "name": member["name"],
                    "source": source,
                    "downloaded": True,
                }
            ],
            "observed_at": iso_now(),
            "content_fetched": True,
            "source_rechecked": True,
            "atomic_snapshot": False,
            "content_executed": False,
            "end_time_ms": None,
        }
    except (subprocess.TimeoutExpired, OutputLimitError):
        raise ProjectReadError("attachment_transfer_timeout") from None
    finally:
        if stage is not None and not accepted:
            shutil.rmtree(stage)
        os.close(lock_fd)


def verified_file(path, receipt):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    stream = os.fdopen(fd, "rb")
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size != receipt["size_bytes"]
            or hashlib.file_digest(stream, "sha256").hexdigest() != receipt["sha256"]
        ):
            raise ValueError("attachment file verification failed")
        stream.seek(0)
        return stream
    except Exception:
        stream.close()
        raise


def open_artifact(conn, config, *, actor, activity_id):
    from . import project_bug_grants as grants
    from . import project_bugs as bugs
    from .project_activity import _get
    from .project_refresh import RefreshBlocked, _selection

    row = _get(conn, actor, activity_id)
    if row["state"] != "succeeded" or row["kind"] != "attachment_download":
        raise ValueError("verified attachment required")
    grants.require_bug_read(
        conn, bugs._bug(conn, row["bug_id"]), actor=actor, grant_id=row["grant_id"]
    )
    try:
        _selection(conn, config, bugs._bug(conn, row["bug_id"]), actor)
    except RefreshBlocked:
        raise PermissionError("attachment reading is paused") from None
    receipt = json.loads(row["result_json"])["items"][0]
    if not re.fullmatch(r"file-[a-z0-9_]{8}", receipt["artifact_id"]):
        raise ValueError("invalid artifact identity")
    directory = storage(config) / receipt["artifact_id"]
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("invalid artifact storage")
    stream = verified_file(directory / "content", receipt)
    try:
        grants.require_bug_read(
            conn, bugs._bug(conn, row["bug_id"]), actor=actor, grant_id=row["grant_id"]
        )
        bugs._event(
            conn,
            row["bug_id"],
            actor,
            "attachment_delivery_authorized",
            {"activity_id": activity_id, "sha256": receipt["sha256"]},
        )
        return stream, receipt
    except Exception:
        stream.close()
        raise
