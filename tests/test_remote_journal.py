import hashlib
import json
import shlex
import subprocess
from uuid import uuid4

import pytest

from k3_support.broker_process import run_process
from k3_support.remote_guard import wrap
from k3_support.remote_journal import RemoteJournal, read_receipt


def test_guard_persists_bound_exit_without_command_body_and_refuses_reexecution(tmp_path):
    tmp_path.chmod(0o700)
    request = str(uuid4())
    command = "printf private-output; exit 7"
    argv = shlex.split(wrap(command, receipt_directory=str(tmp_path), request_id=request))
    result = run_process(argv=argv, cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"},
                         stdin=b"", heartbeat=lambda: None, timeout=10,
                         heartbeat_interval=.1, keepalive=True, detailed=True)
    assert result["exit_code"] == 7 and result["stdout"] == "private-output"
    receipt = json.loads((tmp_path / (request + ".result")).read_text())
    assert receipt == {"version": 1, "request_id": request,
                       "command_digest": hashlib.sha256(command.encode()).hexdigest(), "guard_exit_code": 7}
    assert "private-output" not in (tmp_path / (request + ".intent")).read_text()
    replay = subprocess.run(argv, input=b"", capture_output=True, timeout=5, check=False)
    assert replay.returncode != 0 and replay.stdout == b""
    assert json.loads((tmp_path / (request + ".result")).read_text()) == receipt
    assert read_receipt(str(tmp_path), request, receipt["command_digest"]) == {"state": "guardian_returned", **receipt}


def test_expired_guard_creates_receipt_but_never_executes(tmp_path):
    tmp_path.chmod(0o700)
    request = str(uuid4())
    marker = tmp_path / "not-executed"
    result = subprocess.run(shlex.split(wrap("touch " + shlex.quote(str(marker)),
                            receipt_directory=str(tmp_path), request_id=request)),
                            input=b"1000000000000\n", capture_output=True, timeout=5, check=False)
    assert result.returncode == 124 and not marker.exists()
    assert json.loads((tmp_path / (request + ".result")).read_text())["guard_exit_code"] == 124


def test_journal_crash_intent_and_duplicate_finish_are_not_overwritten(tmp_path):
    tmp_path.chmod(0o700)
    request = str(uuid4())
    journal = RemoteJournal(str(tmp_path), request, "true")
    journal.close()
    with pytest.raises(FileExistsError):
        RemoteJournal(str(tmp_path), request, "different command")
    assert not (tmp_path / (request + ".result")).exists()
    journal = RemoteJournal(str(tmp_path), str(uuid4()), "true")
    try:
        journal.finish(0)
        with pytest.raises(FileExistsError):
            journal.finish(1)
    finally:
        journal.close()


def test_journal_rejects_shared_directory_and_symlink(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        RemoteJournal(str(tmp_path), str(uuid4()), "true")
    tmp_path.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        RemoteJournal(str(link), str(uuid4()), "true")


def test_read_receipt_missing_partial_wrong_binding_and_nonregular_are_not_evidence(tmp_path):
    tmp_path.chmod(0o700)
    request = str(uuid4())
    digest = hashlib.sha256(b"true").hexdigest()
    assert read_receipt(str(tmp_path), request, digest) == {"state": "unknown"}
    assert list(tmp_path.iterdir()) == []
    journal = RemoteJournal(str(tmp_path), request, "true")
    try:
        assert read_receipt(str(tmp_path), request, digest) == {"state": "unknown"}
        with pytest.raises(ValueError, match="intent mismatch"):
            read_receipt(str(tmp_path), request, "0" * 64)
        journal.finish(0)
    finally:
        journal.close()
    result = tmp_path / (request + ".result")
    result.write_text("{")
    with pytest.raises(ValueError):
        read_receipt(str(tmp_path), request, digest)
    result.unlink()
    import os
    os.mkfifo(result, 0o600)
    with pytest.raises(ValueError, match="invalid receipt"):
        read_receipt(str(tmp_path), request, digest)


def test_symlinked_ancestor_cannot_redirect_journal_or_reader(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    receipts = actual / "receipts"
    receipts.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    request = str(uuid4())
    with pytest.raises(ValueError, match="canonical"):
        RemoteJournal(str(alias / "receipts"), request, "true")
    with pytest.raises(ValueError, match="canonical"):
        read_receipt(str(alias / "receipts"), request, "0" * 64)
    assert list(receipts.iterdir()) == []
