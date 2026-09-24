import sqlite3

import pytest

from k3_support.replay_snapshot import SnapshotLimitError, replay_snapshot


def test_recorded_schema_copy_does_not_run_current_migrations(tmp_path, monkeypatch):
    path = tmp_path / 'recorded.db'
    source = sqlite3.connect(path)
    source.execute('CREATE TABLE historical(body TEXT)')
    source.execute("INSERT INTO historical VALUES ('recorded content')")
    source.execute('PRAGMA user_version=7')
    source.commit()
    source.close()
    before = path.read_bytes()
    monkeypatch.setattr('k3_support.replay_snapshot.migrate',
                        lambda conn: pytest.fail('current schema applied to historical snapshot'))
    with replay_snapshot(path, upgrade_schema=False) as snapshot:
        assert snapshot.execute('PRAGMA user_version').fetchone()[0] == 7
        assert snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()[0][0] == 'historical'
        assert snapshot.execute('SELECT body FROM historical').fetchone()[0] == 'recorded content'
        snapshot.execute('DELETE FROM historical')
    assert path.read_bytes() == before


def test_unmigrated_snapshot_still_checks_reference_integrity(tmp_path):
    path = tmp_path / 'invalid.db'
    source = sqlite3.connect(path)
    source.executescript('CREATE TABLE parent(id PRIMARY KEY); '
                         'CREATE TABLE child(parent REFERENCES parent(id)); '
                         'INSERT INTO child VALUES(42);')
    source.close()
    with pytest.raises(ValueError, match='invalid references'), replay_snapshot(path, upgrade_schema=False):
        pytest.fail('invalid historical references accepted')


def test_snapshot_migrates_only_memory_and_discards_writes(tmp_path):
    path = tmp_path / 'source.db'
    source = sqlite3.connect(path)
    source.execute('CREATE TABLE private_fixture(body TEXT)')
    source.execute("INSERT INTO private_fixture VALUES ('private chat')")
    source.commit()
    source.close()
    before = path.read_bytes()
    with replay_snapshot(path) as snapshot:
        assert snapshot.execute('PRAGMA database_list').fetchone()[2] == ''
        assert snapshot.execute('SELECT body FROM private_fixture').fetchone()[0] == 'private chat'
        assert snapshot.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] > 0
        snapshot.execute('DELETE FROM private_fixture')
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(sqlite3.ProgrammingError):
        snapshot.execute('SELECT 1')


def test_missing_source_is_not_created(tmp_path):
    path = tmp_path / 'absent.db'
    with pytest.raises(sqlite3.OperationalError), replay_snapshot(path):
        pytest.fail('missing source accepted')
    assert not path.exists()


def test_snapshot_closes_on_execution_failure(tmp_path):
    path = tmp_path / 'source.db'
    sqlite3.connect(path).close()
    with pytest.raises(RuntimeError, match='execution failed'), replay_snapshot(path) as snapshot:
        raise RuntimeError('execution failed')
    with pytest.raises(sqlite3.ProgrammingError):
        snapshot.execute('SELECT 1')


@pytest.mark.parametrize('options', [
    {'max_bytes': True}, {'max_bytes': 0}, {'max_bytes': 2**31},
    {'timeout_seconds': True}, {'timeout_seconds': float('nan')},
    {'timeout_seconds': float('inf')}, {'timeout_seconds': 0},
    {'upgrade_schema': 'false'}, {'upgrade_schema': None}, {'upgrade_schema': 0},
])
def test_invalid_resource_limits_fail_before_open(tmp_path, options):
    with pytest.raises(ValueError), replay_snapshot(tmp_path / 'absent', **options):
        pytest.fail('invalid limits accepted')
    assert list(tmp_path.iterdir()) == []


def test_oversized_source_rejected_without_change(tmp_path):
    path = tmp_path / 'large.db'
    source = sqlite3.connect(path)
    source.execute('CREATE TABLE fixture(body BLOB)')
    source.execute('INSERT INTO fixture VALUES (zeroblob(65536))')
    source.commit()
    source.close()
    before = path.read_bytes()
    with pytest.raises(SnapshotLimitError, match='size limit'), replay_snapshot(path, max_bytes=4096):
        pytest.fail('oversized source accepted')
    assert path.read_bytes() == before


def test_copy_deadline_stops_before_yield(tmp_path, monkeypatch):
    path = tmp_path / 'source.db'
    sqlite3.connect(path).close()
    ticks = iter([0, 0, 31])
    monkeypatch.setattr('k3_support.replay_snapshot.time.monotonic', lambda: next(ticks))
    with pytest.raises(SnapshotLimitError, match='timed out'), replay_snapshot(path):
        pytest.fail('expired preparation yielded a connection')
    assert path.stat().st_size == 0


def test_page_limit_still_applies_to_replay_writes(tmp_path):
    path = tmp_path / 'source.db'
    sqlite3.connect(path).close()
    with replay_snapshot(path, max_bytes=4 * 1024 * 1024) as snapshot:
        snapshot.execute('CREATE TABLE oversized(body BLOB)')
        with pytest.raises(sqlite3.OperationalError, match='full'):
            snapshot.execute('INSERT INTO oversized VALUES (zeroblob(8388608))')
    assert path.stat().st_size == 0
