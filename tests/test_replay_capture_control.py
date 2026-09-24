import pytest

from k3_support import replay_archive_control as control


def stopped(conn):
    from k3_support.runtime_control import ensure_global_state
    ensure_global_state(conn)
    conn.execute("UPDATE global_control_state SET mode='stopped' WHERE scope='feishu_support'")


def test_capture_requires_stopped_and_confirmation(conn, config, monkeypatch):
    monkeypatch.setattr(control, 'capture', lambda *a, **k: pytest.fail('must not copy'))
    args = dict(name='one', event={}, proposal={}, confirmed=True, writers_quiesced=True)
    for changes in ({'confirmed': 1}, {'writers_quiesced': False}, {'name': '../x'}, {'event': []}):
        with pytest.raises(ValueError):
            control.capture_current(conn, config, **(args | changes))
    with pytest.raises(ValueError, match='fully stop'):
        control.capture_current(conn, config, **args)
    assert not conn.in_transaction
    assert not (config.data_dir/'replay-archives').exists()


def test_capture_fixed_paths_lock_and_no_automatic_run(conn, config, monkeypatch):
    stopped(conn)
    before = conn.serialize()
    calls = []
    def capture(database, request, **kwargs):
        assert conn.in_transaction
        assert request == {'config': config.raw, 'event': {'content':'fixture'}, 'proposal': {}}
        assert database == config.database_path
        assert kwargs['output'] == config.data_dir/'replay-archives'/'one'
        assert kwargs['package'].name == 'k3_support'
        assert kwargs['site_packages'].name in {'site-packages','dist-packages'}
        assert kwargs['timeout'] == 30
        calls.append(1)
        with pytest.raises(ValueError, match='already running'):
            control.run_selected(config.data_dir/'replay-archives', name='one', manifest_digest='a'*64, confirmed=True)
        return {'fixture': True}
    monkeypatch.setattr(control, 'capture', capture)
    result = control.capture_current(conn, config, name='one', event={'content':'fixture'}, proposal={}, confirmed=True, writers_quiesced=True)
    assert len(result['manifest_digest']) == 64 and result['release_authorized'] is False
    assert result['external_writers_independently_verified'] is False
    assert calls == [1] and not conn.in_transaction and conn.serialize() == before


def test_capture_failure_releases_transaction_and_slot(conn, config, monkeypatch):
    stopped(conn)
    def fail(*args, **kwargs):
        raise OSError('fixture')
    monkeypatch.setattr(control, 'capture', fail)
    for _ in range(2):
        with pytest.raises(OSError):
            control.capture_current(conn, config, name='one', event={}, proposal={}, confirmed=True, writers_quiesced=True)
        assert not conn.in_transaction


def test_capture_real_artifacts_under_reserved_database_snapshot(conn, config, tmp_path, monkeypatch):
    import json
    from test_replay_recorded import setup
    from k3_support.ids import digest
    stopped(conn)
    package, dependencies, request, _ = setup(tmp_path, config)
    monkeypatch.setattr(control, '__file__', str(package/'replay_archive_control.py'))
    monkeypatch.setattr(control.sysconfig, 'get_paths', lambda: {'purelib': str(dependencies)})
    before = conn.serialize()
    result = control.capture_current(conn, config, name='real-copy', event=request['event'],
                                    proposal=request['proposal'], confirmed=True, writers_quiesced=True)
    archive = config.data_dir/'replay-archives'/'real-copy'
    manifest = json.loads((archive/'manifest.json').read_text())
    assert result['manifest_digest'] == digest(manifest)
    assert (archive/'k3_support/__init__.py').read_bytes() == (package/'__init__.py').read_bytes()
    assert json.loads((archive/'request.json').read_text()) == request
    assert conn.serialize() == before and not conn.in_transaction
    with pytest.raises(FileExistsError):
        control.capture_current(conn, config, name='real-copy', event=request['event'],
                                proposal={}, confirmed=True, writers_quiesced=True)
    assert digest(json.loads((archive/'manifest.json').read_text())) == result['manifest_digest']
