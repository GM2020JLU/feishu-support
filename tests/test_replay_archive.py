import json
import stat

import pytest

from k3_support import replay_archive, replay_recorded
from k3_support.ids import digest
from test_replay_recorded import setup


def test_capture_copies_exact_private_artifacts(conn, config, tmp_path):
    package, deps, request, expected = setup(tmp_path, config)
    output = tmp_path/'archive'
    before = conn.serialize()
    result = replay_archive.capture(config.database_path, request, package=package,
                                    site_packages=deps, output=output)
    assert result['expected'] == expected
    assert json.loads((output/'manifest.json').read_text()) == result
    assert json.loads((output/'request.json').read_text()) == request
    assert (output/'k3_support/__init__.py').read_bytes() == (package/'__init__.py').read_bytes()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output/'snapshot.db').stat().st_mode) == 0o600
    assert conn.serialize() == before
    with pytest.raises(FileExistsError):
        replay_archive.capture(config.database_path, request, package=package,
                               site_packages=deps, output=output)


def test_copy_failure_never_publishes_manifest(conn, config, tmp_path, monkeypatch):
    package, deps, request, _ = setup(tmp_path, config)
    output = tmp_path/'archive'
    def fail(*args):
        raise OSError('injected disk failure')
    monkeypatch.setattr(replay_archive, '_copy', fail)
    with pytest.raises(OSError, match='disk failure'):
        replay_archive.capture(config.database_path, request, package=package,
                               site_packages=deps, output=output)
    assert output.is_dir()
    assert not (output/'manifest.json').exists()


def test_capture_rejects_nested_destination(conn, config, tmp_path):
    package, deps, request, _ = setup(tmp_path, config)
    with pytest.raises(ValueError, match='inside'):
        replay_archive.capture(config.database_path, request, package=package,
                               site_packages=deps, output=package/'archive')


def test_archive_roundtrip_validates_copied_runtime(conn, config, tmp_path, monkeypatch):
    package, deps, request, _ = setup(tmp_path, config)
    output = tmp_path/'archive'
    manifest = replay_archive.capture(config.database_path, request, package=package,
                                     site_packages=deps, output=output)
    monkeypatch.setattr(replay_recorded, '_run_snapshot_data', lambda *a, **k: {'fixture': True})
    result = replay_archive.replay(output, manifest_digest=digest(manifest))
    assert result['result'] == {'fixture': True}
    assert result['release_authorized'] is False
    (output/'k3_support/__init__.py').write_text('# tampered')
    with pytest.raises(ValueError, match='runtime differs'):
        replay_archive.replay(output, manifest_digest=digest(manifest))


@pytest.mark.parametrize('filename', ['manifest.json', 'request.json'])
def test_archive_tampering_rejected(conn, config, tmp_path, monkeypatch, filename):
    package, deps, request, _ = setup(tmp_path, config)
    output = tmp_path/'archive'
    manifest = replay_archive.capture(config.database_path, request, package=package,
                                     site_packages=deps, output=output)
    monkeypatch.setattr(replay_recorded, '_run_snapshot_data', lambda *a, **k: pytest.fail('must not run'))
    (output/filename).write_text('{}')
    with pytest.raises(ValueError):
        replay_archive.replay(output, manifest_digest=digest(manifest))


def test_archive_symlink_rejected(conn, config, tmp_path):
    package, deps, request, _ = setup(tmp_path, config)
    output = tmp_path/'archive'
    manifest = replay_archive.capture(config.database_path, request, package=package,
                                     site_packages=deps, output=output)
    link = tmp_path/'link'
    link.symlink_to(output, target_is_directory=True)
    with pytest.raises(OSError):
        replay_archive.replay(link, manifest_digest=digest(manifest))


def test_catalog_only_reads_metadata_and_never_confers_trust(conn, config, tmp_path, monkeypatch):
    package, deps, request, _ = setup(tmp_path, config)
    root = tmp_path/'catalog'
    root.mkdir()
    manifest = replay_archive.capture(config.database_path, request, package=package,
                                     site_packages=deps, output=root/'one')
    (root/'partial').mkdir()
    (root/'linked').symlink_to(root/'one', target_is_directory=True)
    original = replay_archive._read_json
    def read(path, limit):
        assert path.name == 'manifest.json'
        return original(path, limit)
    monkeypatch.setattr(replay_archive, '_read_json', read)
    result = replay_archive.catalog(root)
    assert result['content_read'] is False
    assert result['truncated'] is False
    assert [i['name'] for i in result['items']] == ['one', 'partial']
    first, partial = result['items']
    assert first['runtime_digest'] == manifest['expected']['runtime']
    assert first['status'] == 'manifest_present_unverified'
    assert first['trusted'] is False and first['replay_ready'] is False
    assert partial['status'] == 'incomplete'
    assert replay_archive.catalog(root, limit=1)['truncated'] is True


@pytest.mark.parametrize('expected', ['private-content', ['runtime','snapshot','request']])
def test_catalog_does_not_echo_invalid_manifest_content(tmp_path, expected):
    root = tmp_path/'catalog'
    root.mkdir()
    archive = root/'invalid'
    archive.mkdir()
    (archive/'manifest.json').write_text(json.dumps({'schema':'k3-replay-archive-v1','expected': expected}))
    result = replay_archive.catalog(root)
    assert 'private-content' not in json.dumps(result)
    assert result['items'][0]['status'] == 'unreadable_or_invalid'


@pytest.mark.parametrize('replacement', ['symlink', 'fifo', 'changed'])
def test_snapshot_rejected_before_sqlite_or_runner(conn, config, tmp_path, monkeypatch, replacement):
    import os
    package, deps, request, _ = setup(tmp_path, config)
    output = tmp_path/'archive'
    manifest = replay_archive.capture(config.database_path, request, package=package,
                                     site_packages=deps, output=output)
    snapshot = output/'snapshot.db'
    saved = output/'saved.db'
    snapshot.rename(saved)
    if replacement == 'symlink':
        snapshot.symlink_to(saved)
    elif replacement == 'fifo':
        os.mkfifo(snapshot)
    else:
        snapshot.write_bytes(b'changed')
    monkeypatch.setattr(replay_archive, 'run_recorded', lambda *a, **k: pytest.fail('must not open SQLite'))
    from k3_support.operations import OperationsError
    with pytest.raises((OSError, ValueError, OperationsError)):
        replay_archive.replay(output, manifest_digest=digest(manifest))
