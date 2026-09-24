import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from k3_support import broker_deployment_doctor as doctor


def test_missing_deployment_is_reported_without_creating_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.pwd, 'getpwnam', lambda name: (_ for _ in ()).throw(KeyError(name)))
    monkeypatch.setattr(doctor, '_service', lambda *a: (_ for _ in ()).throw(doctor.Unverified('service_not_running')))
    before = list(tmp_path.iterdir())
    result = doctor.inspect(database=str(tmp_path/'private.db'), release_directory=str(tmp_path/'release'))
    assert result['read_only'] and not result['metadata_checks_passed']
    assert result['checks'][0]['reason'] == 'account_missing'
    assert list(tmp_path.iterdir()) == before


@pytest.mark.parametrize('same_uid', [True, False])
def test_no_catalog_read_with_missing_or_shared_identity(monkeypatch, same_uid):
    monkeypatch.setattr(doctor, '_account', lambda name: 1000 if same_uid else (_ for _ in ()).throw(doctor.Unverified('missing')))
    monkeypatch.setattr(doctor, '_path', lambda *a, **kw: None)
    monkeypatch.setattr(doctor, '_service', lambda *a: None)
    monkeypatch.setattr(doctor.os, 'open', lambda *a, **kw: pytest.fail('must not read catalog'))
    result = doctor.inspect(database='/private/db', release_directory='/opt/release')
    assert not result['metadata_checks_passed']


@pytest.mark.parametrize('mutation,reason', [
    ({'st_mode': stat.S_IFLNK | 0o777}, 'symlink_not_accepted'),
    ({'st_mode': stat.S_IFREG | 0o666}, 'excess_permissions'),
    ({'st_nlink': 2}, 'hardlink_not_accepted'),
    ({'st_uid': 2000}, 'wrong_type_or_owner'),
    ({'acl': True}, 'acl_requires_review'),
])
def test_rejects_unsafe_metadata_without_reading_contents(monkeypatch, mutation, reason):
    values = dict(st_mode=stat.S_IFREG | 0o600, st_uid=1000, st_nlink=1)
    values.update({k:v for k,v in mutation.items() if k != 'acl'})
    monkeypatch.setattr(Path, 'lstat', lambda p: SimpleNamespace(**values) if str(p)=='/private/db' else
                        SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0))
    monkeypatch.setattr(os, 'listxattr', lambda p, **kw: ['system.posix_acl_access'] if str(p)=='/private/db' and mutation.get('acl') else [])
    with pytest.raises(doctor.Unverified, match=reason):
        doctor._path('/private/db', owners={1000}, kind='file', private=True)


def test_root_ancestors_are_accepted_for_private_control_file(monkeypatch):
    monkeypatch.setattr(Path, 'lstat', lambda p: SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=1000, st_nlink=1)
                        if str(p)=='/var/lib/control/db' else SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0))
    monkeypatch.setattr(os, 'listxattr', lambda *a, **kw: [])
    assert doctor._path('/var/lib/control/db', owners={1000}, kind='file', private=True).st_uid==1000


@pytest.mark.parametrize('uids,accepted', [('1001 1001 1001 1001', True), ('0 0 0 0', False)])
def test_service_checks_real_process_uid(monkeypatch, uids, accepted):
    monkeypatch.setattr(doctor.subprocess, 'run', lambda argv, **kw: SimpleNamespace(returncode=0,
                        stdout='Id=fixture.service\nLoadState=loaded\nActiveState=active\nMainPID=1234\n'))
    monkeypatch.setattr(Path, 'read_text', lambda p: 'Uid:\t'+uids+'\n')
    if accepted:
        doctor._service('fixture.service',1001)
    else:
        with pytest.raises(doctor.Unverified, match='process_identity_mismatch'):
            doctor._service('fixture.service',1001)


def test_cli_does_not_load_config_or_open_database(monkeypatch, capsys):
    from k3_support.cli import main
    monkeypatch.setattr('k3_support.cli._config', lambda *a: pytest.fail('config read'))
    monkeypatch.setattr('k3_support.cli._conn', lambda *a: pytest.fail('database opened'))
    seen=[]
    monkeypatch.setattr(doctor, 'inspect', lambda **kw: seen.append(kw) or {'read_only':True})
    assert main(['broker-deployment-doctor','--database','/private/db','--release-directory','/opt/release'])==0
    assert json.loads(capsys.readouterr().out)=={'read_only':True}
    assert seen[0]['database']=='/private/db'
