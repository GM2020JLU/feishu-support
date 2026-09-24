import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from k3_support import replay_recorded as recorded
from k3_support.ids import digest


def setup(tmp_path, config):
    package = tmp_path/'k3_support'
    deps = tmp_path/'site-packages'
    package.mkdir()
    deps.mkdir()
    (package/'__init__.py').write_text('# recorded package')
    (deps/'dependency.txt').write_text('recorded dependency')
    request = {'config': config.raw, 'event': {'content': 'synthetic'}, 'proposal': {}}
    identity = recorded.runtime_identity(package=package, site_packages=deps, deadline=time.monotonic()+30)
    data = recorded._snapshot_data(config.database_path, upgrade_schema=False)
    expected = {'runtime': digest(identity), 'snapshot': hashlib.sha256(data).hexdigest(),
                'request': digest(request)}
    return package, deps, request, expected


def test_recorded_paths_and_unchanged_schema_reach_runner(conn, config, tmp_path, monkeypatch):
    package, deps, request, expected = setup(tmp_path, config)
    def run(data, value, **kwargs):
        assert kwargs['package'] == package and kwargs['site_packages'] == deps
        assert digest(value) == expected['request']
        assert hashlib.sha256(data).hexdigest() == expected['snapshot']
        return {'fixture': True}
    monkeypatch.setattr(recorded, '_run_snapshot_data', run)
    before = conn.serialize()
    result = recorded.run_recorded(config.database_path, request, package=package,
                                   site_packages=deps, expected=expected)
    assert result['capture_time_verified'] is False and result['release_authorized'] is False
    assert conn.serialize() == before


@pytest.mark.parametrize('part', ['runtime', 'snapshot', 'request'])
def test_mismatches_fail_before_execution(conn, config, tmp_path, monkeypatch, part):
    package, deps, request, expected = setup(tmp_path, config)
    expected[part] = '0'*64
    monkeypatch.setattr(recorded, '_run_snapshot_data', lambda *a, **k: pytest.fail('must not run'))
    with pytest.raises(ValueError, match='differs'):
        recorded.run_recorded(config.database_path, request, package=package,
                              site_packages=deps, expected=expected)


def test_changed_runtime_rejects_output(conn, config, tmp_path, monkeypatch):
    package, deps, request, expected = setup(tmp_path, config)
    def run(*args, **kwargs):
        (package/'__init__.py').write_text('# changed')
        return {'fixture': True}
    monkeypatch.setattr(recorded, '_run_snapshot_data', run)
    with pytest.raises(ValueError, match='changed'):
        recorded.run_recorded(config.database_path, request, package=package,
                              site_packages=deps, expected=expected)


def test_snapshot_receives_remaining_budget(conn, config, tmp_path, monkeypatch):
    package, deps, request, expected = setup(tmp_path, config)
    identity = recorded.runtime_identity(package=package, site_packages=deps,
                                        deadline=time.monotonic()+30)
    clock = iter([100.0, 102.0, 103.0, 104.0])
    monkeypatch.setattr(recorded.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(recorded, 'runtime_identity', lambda **kwargs: identity)
    data = b'fixture snapshot'
    expected['snapshot'] = hashlib.sha256(data).hexdigest()
    def snapshot(database, **kwargs):
        assert kwargs == {'upgrade_schema': False, 'timeout_seconds': 8.0}
        return data
    def run(*args, **kwargs):
        assert kwargs['timeout'] == 7.0
        return {}
    monkeypatch.setattr(recorded, '_snapshot_data', snapshot)
    monkeypatch.setattr(recorded, '_run_snapshot_data', run)
    recorded.run_recorded(config.database_path, request, package=package,
                          site_packages=deps, expected=expected, timeout=10)


@pytest.mark.parametrize('entrypoint', ['direct', 'archive_cli', 'archive_control'])
def test_real_namespace_uses_selected_package_dependency_and_recorded_schema(conn, config, tmp_path, entrypoint):
    conn.execute('PRAGMA user_version=7')
    package, deps, request, expected = setup(tmp_path, config)
    (deps/'recorded_dependency.py').write_text("VERSION='recorded-dependency-v1'\n")
    (package/'replay_history.py').write_text('''import sqlite3
from recorded_dependency import VERSION
def execute_snapshot(request, probe=False):
    conn=sqlite3.connect(':memory:')
    try:
        with open('/replay/snapshot.db','rb') as source:
            conn.deserialize(source.read())
        return {'package':'recorded-code-v1','dependency':VERSION,
                'schema':conn.execute('PRAGMA user_version').fetchone()[0]}
    finally:
        conn.close()
''')
    expected['runtime'] = digest(recorded.runtime_identity(package=package, site_packages=deps,
                                                         deadline=time.monotonic()+30))
    before = conn.serialize()
    # An isolated interpreter exercises bwrap without relaxing the suite-wide
    # transport guard. Only synthetic inputs and explicit fixture trees enter it.
    program = '''import json, sys
sys.path.insert(0, sys.argv[1])
from k3_support.replay_recorded import run_recorded
value=json.load(sys.stdin)
print(json.dumps(run_recorded(**value)))
'''
    payload = json.dumps(dict(database=str(config.database_path), request=request,
                              package=str(package), site_packages=str(deps), expected=expected))
    if entrypoint != 'direct':
        program = '''import contextlib, io, json, sys
sys.path.insert(0, sys.argv[1])
from k3_support.cli import main
value=json.load(sys.stdin)
archive=value['package']+'-archive'
sys.stdin=io.TextIOWrapper(io.BytesIO(json.dumps(value['request']).encode()))
out=io.StringIO()
with contextlib.redirect_stdout(out):
    status=main(['--config','/nonexistent/config', 'replay-archive-capture',
                 '--database',value['database'],'--package',value['package'],
                 '--site-packages',value['site_packages'],'--output',archive])
assert status == 0
receipt=json.loads(out.getvalue())
if sys.argv[2] == 'archive_control':
    from pathlib import Path
    from k3_support.replay_archive_control import run_selected
    selected=Path(archive)
    print(json.dumps(run_selected(selected.parent, name=selected.name,
                                 manifest_digest=receipt['manifest_digest'], confirmed=True)))
    raise SystemExit(0)
raise SystemExit(main(['--config','/nonexistent/config','replay-archive-run',
                      '--archive',archive,'--manifest-digest',receipt['manifest_digest']]))
'''
    output = subprocess.run(
        [sys.executable, '-I', '-B', '-c', program,
         str(Path(recorded.__file__).resolve().parents[1]), entrypoint],
        input=payload,
        text=True, capture_output=True, timeout=40, cwd=tmp_path, env={})
    assert output.returncode == 0, output.stderr
    result = json.loads(output.stdout)
    assert result['result'] == {'package':'recorded-code-v1',
        'dependency':'recorded-dependency-v1', 'schema':7}
    assert result['release_authorized'] is False
    assert result['capture_time_verified'] is False
    assert conn.serialize() == before
