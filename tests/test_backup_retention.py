import pytest

from k3_support.operations import OperationsError, backup_database, prune_backups


@pytest.mark.parametrize('daily,weekly', [(0, 8), (-1, 8), (True, 8), (1, -1), (1, False), (3651, 8)])
def test_invalid_backup_retention_never_creates_directory(config, daily, weekly):
    with pytest.raises(OperationsError):
        prune_backups(config, keep_daily=daily, keep_weekly=weekly)
    assert not (config.data_dir / 'backups').exists()


def test_backup_missing_source_does_not_create_empty_instance(config):
    with pytest.raises(OperationsError, match='existing regular source'):
        backup_database(config)
    assert not config.database_path.exists()
    assert not (config.data_dir / 'backups').exists()


def test_backup_filename_normalizes_timezone(conn, config):
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    result = backup_database(config, now=datetime(2026, 9, 8, 7, tzinfo=timezone(timedelta(hours=8))))
    assert Path(result['path']).name == 'support-20260907T230000Z.db'


def test_naive_backup_time_is_rejected_before_io(config):
    from datetime import datetime

    with pytest.raises(OperationsError, match='timezone-aware'):
        backup_database(config, now=datetime(2026, 9, 8))  # noqa: DTZ001 - rejection fixture
    assert not (config.data_dir / 'backups').exists()


def test_failed_integrity_never_publishes_backup(conn, config, monkeypatch):
    monkeypatch.setattr('k3_support.operations.integrity', lambda _: {'ok': False})
    with pytest.raises(OperationsError, match='integrity'):
        backup_database(config)
    assert list((config.data_dir / 'backups').iterdir()) == []


def test_backup_timeout_does_not_publish_partial_copy(conn, config, monkeypatch):
    ticks = iter([0.0, 121.0])
    monkeypatch.setattr('k3_support.operations.time.monotonic', lambda: next(ticks, 121.0))
    with pytest.raises(OperationsError, match='deadline exceeded'):
        backup_database(config)
    assert list((config.data_dir / 'backups').iterdir()) == []


def test_backup_times_out_under_real_exclusive_sqlite_lock(conn, config):
    import time

    assert conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
    conn.execute('BEGIN EXCLUSIVE')
    started = time.monotonic()
    try:
        with pytest.raises(OperationsError, match='deadline exceeded'):
            backup_database(config, timeout_seconds=0.1)
        assert conn.in_transaction
        assert time.monotonic() - started < 2
        assert list((config.data_dir / 'backups').iterdir()) == []
    finally:
        conn.rollback()
    assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


@pytest.mark.parametrize('timeout', [0, -1, True, float('nan'), float('inf'), 3601])
def test_backup_rejects_invalid_timeout_before_io(config, timeout):
    with pytest.raises(OperationsError, match='timeout'):
        backup_database(config, timeout_seconds=timeout)
    assert not (config.data_dir / 'backups').exists()


def test_backup_publication_does_not_overwrite_racing_file(conn, config, monkeypatch):
    import os
    from pathlib import Path

    original = os.link
    def collision(source, target):
        Path(target).write_text('other backup must survive')
        original(source, target)
    monkeypatch.setattr('k3_support.operations.os.link', collision)
    with pytest.raises(FileExistsError):
        backup_database(config)
    files = list((config.data_dir / 'backups').iterdir())
    assert len(files) == 1 and files[0].read_text() == 'other backup must survive'


def test_backup_prune_preserves_unknown_files_and_latest(config):
    root = config.data_dir / 'backups'
    root.mkdir(parents=True)
    names = ['support-20260908T010000Z.db', 'support-20260907T010000Z.db', 'support-invalid.db']
    for name in names:
        (root / name).write_text('synthetic fixture')
    removed = prune_backups(config, keep_daily=1, keep_weekly=0)
    # Unknown names cannot consume the slot intended for the latest valid backup.
    assert (root / names[0]).exists()
    assert (root / names[2]).exists()
    assert removed == [str(root / names[1])]


def test_backup_prune_rejects_redirected_directory(config, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / 'backups').symlink_to(outside, target_is_directory=True)
    with pytest.raises(OperationsError):
        prune_backups(config)


def test_backup_prune_rejects_redirected_ancestor(config, tmp_path):
    from k3_support.config import Config

    outside = tmp_path / 'real-instance'
    (outside / 'backups').mkdir(parents=True)
    alias = tmp_path / 'alias-instance'
    alias.symlink_to(outside, target_is_directory=True)
    raw = dict(config.raw)
    raw['paths'] = {**raw['paths'], 'data_dir': str(alias)}
    redirected = Config(raw, config.path)
    with pytest.raises(OperationsError):
        prune_backups(redirected, dry_run=True)


def test_preview_and_apply_share_selection_without_preview_writes(config):
    root = config.data_dir / 'backups'
    root.mkdir(parents=True)
    for date in ['20260908', '20260907', '20260906']:
        (root / f'support-{date}T010000Z.db').write_text('synthetic')
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    planned = prune_backups(config, keep_daily=1, keep_weekly=2, dry_run=True)
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before
    assert len(planned) == 1
    assert planned == [str(root / 'support-20260907T010000Z.db')]
    assert prune_backups(config, keep_daily=1, keep_weekly=2) == planned


def test_preview_cli_does_not_initialize_instance(config, capsys):
    import json

    import yaml

    from k3_support.cli import main

    config.path.write_text(yaml.safe_dump(config.raw))
    assert main(['--config', str(config.path), 'backup-retention-preview']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['read_only'] and result['candidate_paths'] == []
    assert not config.database_path.exists()
    assert not (config.data_dir / 'backups').exists()


@pytest.mark.parametrize('key,value', [('backup_keep_recent', 0), ('backup_keep_recent', True),
                                     ('backup_keep_weekly', -1), ('backup_keep_weekly', 521)])
def test_backup_config_rejects_invalid_policy(config, key, value):
    import copy

    from k3_support.config import ConfigError, validate_config

    raw = copy.deepcopy(config.raw)
    raw['policy'][key] = value
    with pytest.raises(ConfigError, match=key):
        validate_config(raw)


def test_preview_cli_uses_configured_backup_policy(config, capsys):
    import json

    import yaml

    from k3_support.cli import main

    config.raw['policy'].update(backup_keep_recent=3, backup_keep_weekly=2)
    config.path.write_text(yaml.safe_dump(config.raw))
    assert main(['--config', str(config.path), 'backup-retention-preview']) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result['keep_recent'], result['keep_weekly']) == (3, 2)


@pytest.mark.parametrize('replacement', ['rewrite', 'symlink'])
def test_backup_changed_at_deletion_boundary_is_preserved(config, tmp_path, monkeypatch, replacement):
    from k3_support import retention_recovery

    root = config.data_dir / 'backups'
    root.mkdir(parents=True)
    latest = root / 'support-20260908T010000Z.db'
    old = root / 'support-20260907T010000Z.db'
    latest.write_text('latest synthetic backup')
    old.write_text('old synthetic backup')
    outside = tmp_path / 'replacement-source'
    outside.write_text('replacement must survive')
    original_parent = retention_recovery._parent

    def change_before_open(path):
        if replacement == 'rewrite':
            old.write_text('new content written after retention selection')
        else:
            old.unlink()
            old.symlink_to(outside)
        return original_parent(path)

    monkeypatch.setattr(retention_recovery, '_parent', change_before_open)
    with pytest.raises(OperationsError):
        prune_backups(config, keep_daily=1, keep_weekly=0)
    assert latest.read_text() == 'latest synthetic backup'
    assert outside.read_text() == 'replacement must survive'
    if replacement == 'rewrite':
        assert old.read_text() == 'new content written after retention selection'
    else:
        assert old.is_symlink()
