"""Read-only deployment metadata audit; never activates services or opens the DB."""

import grp
import os
import pwd
import stat
import subprocess
from pathlib import Path

from .broker_catalog import Catalog


class Unverified(ValueError):
    pass


def _metadata(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise Unverified('symlink_not_accepted')
    # An ACL may widen access despite apparently restrictive owner/group bits.
    # Fail closed instead of interpreting a platform-specific ACL incompletely.
    if any(name in {'system.posix_acl_access', 'system.posix_acl_default'}
           for name in os.listxattr(path, follow_symlinks=False)):
        raise Unverified('acl_requires_review')
    return info


def _path(value, *, owners, kind, private=False, executable=False, group_writable=False):
    path = Path(value)
    if not path.is_absolute() or str(path) != str(value) or '..' in path.parts:
        raise Unverified('absolute_normalized_path_required')
    for parent in reversed(path.parents):
        info = _metadata(parent)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in ({0} | owners) or info.st_mode & 0o022:
            raise Unverified('untrusted_parent')
    info = _metadata(path)
    predicate = {'directory': stat.S_ISDIR, 'file': stat.S_ISREG, 'socket': stat.S_ISSOCK}[kind]
    if not predicate(info.st_mode) or info.st_uid not in owners:
        raise Unverified('wrong_type_or_owner')
    if info.st_mode & (0o077 if private else 0o002 if group_writable else 0o022):
        raise Unverified('excess_permissions')
    if kind == 'file' and info.st_nlink != 1:
        raise Unverified('hardlink_not_accepted')
    if executable and not info.st_mode & 0o111:
        raise Unverified('not_executable')
    return info


def _broker_socket(control_user, worker_user, control):
    group = grp.getgrnam('k3-support-ipc').gr_gid
    info = _path('/run/k3-support-broker/broker.sock', owners={control},
                 kind='socket', group_writable=True)
    if info.st_gid != group or stat.S_IMODE(info.st_mode) != 0o660:
        raise Unverified('ipc_socket_permissions')
    for name in (control_user, worker_user):
        account = pwd.getpwnam(name)
        if group not in os.getgrouplist(name, account.pw_gid):
            raise Unverified('ipc_group_membership_missing')


def _account(name):
    try:
        value = pwd.getpwnam(name)
    except KeyError:
        raise Unverified('account_missing') from None
    if value.pw_uid <= 0:
        raise Unverified('non_root_identity_required')
    return value.pw_uid


def _service(unit, uid):
    fields = ('Id', 'LoadState', 'ActiveState', 'MainPID')
    result = subprocess.run(['/usr/bin/systemctl', '--system', 'show', '--no-pager',
                             '--property=' + ','.join(fields), unit],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=5, check=False, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    if result.returncode or len(result.stdout) > 4096:
        raise Unverified('service_observation_unavailable')
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition('=')
        if not separator or key not in fields or key in values:
            raise Unverified('invalid_service_observation')
        values[key] = value
    if (set(values) != set(fields) or values['Id'] != unit or values['LoadState'] != 'loaded'
            or values['ActiveState'] != 'active' or not values['MainPID'].isascii()
            or not values['MainPID'].isdigit() or not 0 < int(values['MainPID']) < 2**31):
        raise Unverified('service_not_running')
    # Observe kernel credentials, not just the service's configured User string.
    proc = Path('/proc') / values['MainPID']
    status = (proc / 'status').read_text()
    ids = [line.split()[1:] for line in status.splitlines() if line.startswith('Uid:')]
    if ids != [[str(uid)] * 4]:
        raise Unverified('process_identity_mismatch')


def inspect(*, database, release_directory, catalog_directory='/etc/k3-support/execution-catalog',
            control_user='k3-support-control', worker_user='k3-support-worker'):
    """Return bounded codes, never config/contract/credential contents or commands."""
    checks = []

    def check(name, operation):
        try:
            value = operation()
        except Unverified as exc:
            checks.append({'name': name, 'status': 'unverified', 'reason': str(exc)})
        except (OSError, ValueError, KeyError, UnicodeError, subprocess.SubprocessError):
            checks.append({'name': name, 'status': 'unverified', 'reason': 'unavailable_or_invalid'})
        else:
            checks.append({'name': name, 'status': 'ok'})
            return value
        return None

    control = check('control_account', lambda: _account(control_user))
    worker = check('worker_account', lambda: _account(worker_user))
    distinct = control is not None and worker is not None and control != worker
    checks.append({'name': 'independent_identities', 'status': 'ok' if distinct else 'unverified'})
    check('protected_release_directory', lambda: _path(release_directory, owners={0}, kind='directory'))
    for entry in ('k3-support-broker', 'k3-support-broker-launcher', 'k3-support-broker-worker',
                  'k3-support-broker-observer', 'k3-support-broker-remote'):
        check('release_entry:' + entry, lambda entry=entry: _path(
            str(Path(release_directory) / 'bin' / entry), owners={0}, kind='file', executable=True))
    if control is not None:
        # Parent privacy protects sidecars/backups created within it; their contents
        # and external backup locations are deliberately not read by this audit.
        check('private_database_directory', lambda: _path(str(Path(database).parent), owners={control},
                                                         kind='directory', private=True))
        check('private_database', lambda: _path(database, owners={control}, kind='file', private=True))
        for unit in ('k3-support-broker.service', 'k3-support-broker-dispatcher.service',
                     'k3-support-broker-remote.service'):
            check(unit, lambda unit=unit: _service(unit, control))
        check('launcher_socket', lambda: _path('/run/k3-support-launcher.sock', owners={control},
                                              kind='socket', private=True))
    check('k3-support-broker-launcher.service', lambda: _service('k3-support-broker-launcher.service', 0))
    if distinct:
        check('broker_socket', lambda: _broker_socket(control_user, worker_user, control))
        def profiles():
            _path(catalog_directory, owners={0, control}, kind='directory')
            fd = os.open(catalog_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                return Catalog(fd, control_uid=control, worker_uid=worker).profiles()
            finally:
                os.close(fd)
        catalog = check('execution_catalog', profiles)
        for profile in catalog or ():
            check('executor:' + profile.name,
                  lambda p=profile: _path(p.executable, owners={0}, kind='file', executable=True))
            check('credential_home:' + profile.name,
                  lambda p=profile: _path(p.agent_home, owners={worker}, kind='directory', private=True))
    return {'read_only': True, 'scope': 'deployment_metadata', 'metadata_checks_passed':
            bool(distinct) and all(row['status'] == 'ok' for row in checks), 'checks': checks,
            'acceptance_still_required': ['worker_access_denial', 'credential_provisioning',
                                         'native_task_lifecycle', 'external_channel_callbacks',
                                         'backup_access_and_recovery', 'device_and_remote_scope'],
            'note': '元数据检查不等于生产就绪；不会读取数据库或凭据内容，也不会启动服务。'}
