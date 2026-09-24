import json
import os

import pytest
from test_backup_prune_audit import NOW, backups

from k3_support.backup_prune_audit import history, inspect_receipt
from k3_support.operations import prune_backups


def uncertain(config, monkeypatch):
    root = backups(config)
    original = os.unlink

    def fail(*args, **kwargs):
        raise OSError("synthetic failure")

    monkeypatch.setattr("k3_support.operations.os.unlink", fail)
    with pytest.raises(OSError):
        prune_backups(config, keep_daily=1, keep_weekly=0, now=NOW)
    monkeypatch.setattr("k3_support.operations.os.unlink", original)
    return root, history(config)["items"][0]


@pytest.mark.parametrize(
    "state", ["same_version", "missing", "different_version", "not_regular"]
)
def test_inspect_does_not_mutate_receipt_or_authorize_retry(
    config, monkeypatch, state, tmp_path
):
    root, receipt = uncertain(config, monkeypatch)
    path = root / receipt["name"]
    if state == "missing":
        path.unlink()
    elif state == "different_version":
        path.write_text("replacement must survive")
    elif state == "not_regular":
        path.unlink()
        path.symlink_to(tmp_path / "must-not-follow")
    journal = root / ".retention-audit" / (receipt["receipt_id"] + ".json")
    before = journal.read_bytes()
    result = inspect_receipt(config, receipt_id=receipt["receipt_id"])
    assert result["target_state"] == state
    assert result["receipt_state"] == "prepared"
    assert result["read_only"] and not result["retry_authorized"]
    assert not result["deletion_cause_verified"]
    assert journal.read_bytes() == before
    if state == "different_version":
        assert path.read_text() == "replacement must survive"


def test_receipt_cannot_redirect_inspection_outside_backups(config, monkeypatch):
    root, receipt = uncertain(config, monkeypatch)
    journal = root / ".retention-audit" / (receipt["receipt_id"] + ".json")
    receipt["name"] = "../../private"
    journal.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="binding"):
        inspect_receipt(config, receipt_id=receipt["receipt_id"])


def test_receipt_change_during_observation_rejected(config, monkeypatch):
    from k3_support import backup_prune_audit as audit

    _, receipt = uncertain(config, monkeypatch)
    original = audit._read_receipt
    calls = []

    def changed(*args):
        value = original(*args)
        calls.append(1)
        if len(calls) == 2:
            value["state"] = "deleted"
        return value

    monkeypatch.setattr(audit, "_read_receipt", changed)
    with pytest.raises(ValueError, match="changed"):
        inspect_receipt(config, receipt_id=receipt["receipt_id"])
