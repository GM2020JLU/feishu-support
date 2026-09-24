from datetime import UTC, datetime

import pytest

from k3_support.backup_prune_audit import history
from k3_support.operations import OperationsError, prune_backups

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def backups(config):
    root = config.data_dir / "backups"
    root.mkdir(parents=True)
    for date in ("20260907", "20260808", "20260701"):
        (root / f"support-{date}T000000Z.db").write_text("synthetic")
    return root


def test_age_overrides_counts_except_latest_and_preview_has_no_receipts(config):
    root = backups(config)
    planned = prune_backups(
        config, keep_daily=20, keep_weekly=20, max_age_days=31, now=NOW, dry_run=True
    )
    assert len(planned) == 2  # Exact 31-day boundary is expired.
    assert not (root / ".retention-audit").exists()
    removed = prune_backups(
        config, keep_daily=20, keep_weekly=20, max_age_days=31, now=NOW
    )
    assert removed == planned
    assert (root / "support-20260907T000000Z.db").exists()
    records = history(config)["items"]
    assert len(records) == 2 and all(row["state"] == "deleted" for row in records)
    assert all(row["policy"]["max_age_days"] == 31 for row in records)
    assert all(
        path.stat().st_mode & 0o777 == 0o600
        for path in (root / ".retention-audit").iterdir()
    )


def test_latest_and_future_backup_survive_even_when_all_normal_backups_expire(config):
    root = backups(config)
    future = root / "support-20990101T000000Z.db"
    future.write_text("future timestamp must not replace latest")
    prune_backups(config, keep_daily=1, keep_weekly=0, max_age_days=1, now=NOW)
    assert future.exists()
    assert (root / "support-20260907T000000Z.db").exists()


def test_failed_unlink_records_uncertainty_and_blocks_automatic_retry(
    config, monkeypatch
):
    import os

    root = backups(config)
    original = os.unlink
    calls = []

    def fail(name, **kwargs):
        calls.append(name)
        raise OSError("synthetic unlink failure")

    monkeypatch.setattr("k3_support.operations.os.unlink", fail)
    with pytest.raises(OSError):
        prune_backups(config, keep_daily=1, keep_weekly=0, now=NOW)
    assert len(calls) == 1
    assert history(config)["items"][0]["state"] == "prepared"
    monkeypatch.setattr("k3_support.operations.os.unlink", original)
    with pytest.raises(FileExistsError):
        prune_backups(config, keep_daily=1, keep_weekly=0, now=NOW)
    assert len(list(root.glob("support-*.db"))) == 3


def test_audit_failure_before_unlink_preserves_backup(config, monkeypatch):
    root = backups(config)

    def fail(*args):
        raise OSError("synthetic journal fsync failure")

    monkeypatch.setattr("k3_support.backup_prune_audit.os.fsync", fail)
    with pytest.raises(OSError):
        prune_backups(config, keep_daily=1, keep_weekly=0, now=NOW)
    assert len(list(root.glob("support-*.db"))) == 3


def test_unreadable_receipt_is_not_success(config):
    root = backups(config) / ".retention-audit"
    root.mkdir()
    (root / ("a" * 64 + ".json")).write_text("{truncated")
    assert history(config)["items"] == [{"receipt_id": "a" * 64, "state": "unreadable"}]


@pytest.mark.parametrize("age", [0, -1, True, 3651])
def test_invalid_age_before_io(config, age):
    with pytest.raises(OperationsError):
        prune_backups(config, max_age_days=age)
    assert not (config.data_dir / 'backups').exists()


def test_failure_after_unlink_remains_unconfirmed(config, monkeypatch):
    root = backups(config)

    def fail(*args, **kwargs):
        raise OSError('synthetic completion publication failure')

    monkeypatch.setattr('k3_support.backup_prune_audit.os.replace', fail)
    with pytest.raises(OSError):
        prune_backups(config, keep_daily=1, keep_weekly=0, now=NOW)
    records = history(config)['items']
    assert len(records) == 1 and records[0]['state'] == 'prepared'
    assert not (root / records[0]['name']).exists()
    assert (root / 'support-20260907T000000Z.db').exists()


def test_redirected_audit_directory_cannot_receive_writes(config, tmp_path):
    root = backups(config)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / '.retention-audit').symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        prune_backups(config, keep_daily=1, keep_weekly=0, now=NOW)
    assert list(outside.iterdir()) == []
    assert len(list(root.glob('support-*.db'))) == 3


def test_history_pagination_and_missing_instance_are_readonly(config):
    assert history(config)['items'] == []
    assert not (config.data_dir / 'backups').exists()
    backups(config)
    prune_backups(config, keep_daily=1, keep_weekly=0, now=NOW)
    first = history(config, limit=1)
    second = history(config, limit=1, after_id=first['next_cursor'])
    assert len(first['items']) == len(second['items']) == 1
    assert first['items'][0]['receipt_id'] != second['items'][0]['receipt_id']
    assert second['next_cursor'] is None


def test_history_cli_never_initializes_instance(config, capsys):
    import json

    import yaml

    from k3_support.cli import main

    config.path.write_text(yaml.safe_dump(config.raw))
    assert main(['--config', str(config.path), 'backup-retention-history']) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['read_only'] and value['items'] == []
    assert not config.database_path.exists()
    assert not (config.data_dir / 'backups').exists()


@pytest.mark.parametrize('age', [0, -1, True, 3651])
def test_invalid_age_config_rejected(config, age):
    import copy

    from k3_support.config import ConfigError, validate_config

    raw = copy.deepcopy(config.raw)
    raw['policy']['backup_max_age_days'] = age
    with pytest.raises(ConfigError, match='backup_max_age_days'):
        validate_config(raw)
