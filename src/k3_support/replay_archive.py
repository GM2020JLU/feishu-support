"""Private, explicitly requested replay captures; no activation or live capture job.

Callers must quiesce writers. Hash checks detect ordinary drift, not hostile
same-UID modification. An absent manifest means incomplete, never replay-ready.
"""
import hashlib
import json
import os
import re
import stat
from pathlib import Path
import time
from datetime import datetime

from .ids import digest
from .recovery_bundle import _copy, _write
from .recovery_inventory import _hash
from .retention_recovery import _parent
from .replay_history import _payload, _snapshot_data, _validate_timeout
from .replay_recorded import runtime_identity, run_recorded
from .timeutil import iso_now


def capture(database, request, *, package, site_packages, output, timeout=300):
    _validate_timeout(timeout)
    deadline = time.monotonic() + timeout
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError('replay capture deadline exceeded')
        return value
    request = json.loads(_payload(request))
    if not isinstance(request, dict) or set(request) != {'config', 'event', 'proposal'}:
        raise ValueError('exact config/event/proposal required')
    package, site_packages, output = map(Path, (package, site_packages, output))
    if not output.is_absolute() or '..' in output.parts or output == Path('/'):
        raise ValueError('absolute non-root capture destination required')
    for source in (package, site_packages):
        if output.resolve().is_relative_to(source.resolve()):
            raise ValueError('capture destination must not be inside a source tree')
    identity = runtime_identity(package=package, site_packages=site_packages, deadline=deadline)
    data = _snapshot_data(Path(database), upgrade_schema=False, timeout_seconds=remaining())
    # Require an existing real parent, and never reuse an existing destination.
    parent, name = _parent(output)
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)
    for source, target in ((package, output/'k3_support'),
                           (site_packages, output/site_packages.name)):
        target.mkdir(mode=0o700)
        count, total = 0, 0
        def fail(error):
            raise error
        for directory, dirs, names in os.walk(source, followlinks=False, onerror=fail):
            if any((Path(directory)/name).is_symlink() for name in dirs):
                raise ValueError('runtime directory symlink rejected')
            for name in names:
                remaining()
                count += 1
                if count > 50000:
                    raise ValueError('runtime file limit exceeded')
                path = Path(directory)/name
                item = _copy(path, target/path.relative_to(source), 1024**3-total)
                total += item['bytes']
    copied = runtime_identity(package=output/'k3_support',
                              site_packages=output/site_packages.name, deadline=deadline)
    current = runtime_identity(package=package, site_packages=site_packages, deadline=deadline)
    if copied != identity or current != identity:
        raise ValueError('runtime changed during capture')
    _write(output/'snapshot.db', data)
    _write(output/'request.json', _payload(request))
    manifest = {'schema': 'k3-replay-archive-v1', 'recorded_at': iso_now(),
                'runtime': identity, 'dependency_directory': site_packages.name,
                'expected': {'runtime': digest(identity),
                             'snapshot': hashlib.sha256(data).hexdigest(),
                             'request': digest(request)},
                'capture_time_verified': False, 'release_authorized': False}
    remaining()
    _write(output/'manifest.json', json.dumps(manifest, sort_keys=True).encode())
    return manifest


def _read_json(path, limit):
    parent, name = _parent(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('regular archive file required')
        data = source.read(limit+1)
        if len(data) > limit:
            raise ValueError('archive input exceeds limit')
    return json.loads(data)


def replay(archive, *, manifest_digest, timeout=30):
    """Use a digest retained outside the archive; do not trust self-signed content."""
    _validate_timeout(timeout)
    deadline = time.monotonic()+timeout
    archive = Path(archive)
    if not archive.is_absolute() or '..' in archive.parts or archive == Path('/'):
        raise ValueError('absolute non-root archive required')
    if not isinstance(manifest_digest, str) or not re.fullmatch('[a-f0-9]{64}', manifest_digest):
        raise ValueError('trusted manifest digest required')
    manifest = _read_json(archive/'manifest.json', 65536)
    if digest(manifest) != manifest_digest:
        raise ValueError('archive manifest differs')
    if (not isinstance(manifest, dict)
            or manifest.get('schema') != 'k3-replay-archive-v1'
            or manifest.get('dependency_directory') not in ('site-packages', 'dist-packages')
            or manifest.get('capture_time_verified') is not False
            or manifest.get('release_authorized') is not False):
        raise ValueError('unsupported archive manifest')
    expected = manifest.get('expected')
    if (not isinstance(expected, dict) or set(expected) != {'runtime', 'snapshot', 'request'}
            or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v)
                   for v in expected.values())):
        raise ValueError('invalid archive bindings')
    # Archive captures contain a self-contained, normalized SQLite image. Check
    # the actual regular file without following links before SQLite opens it.
    # This is ordinary drift protection, not a hostile same-UID race boundary.
    snapshot = _hash(archive/'snapshot.db', 32*1024*1024)
    if snapshot['sha256'] != expected['snapshot']:
        raise ValueError('archive snapshot differs')
    request = _read_json(archive/'request.json', 262144)
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise TimeoutError('archive replay deadline exceeded')
    return run_recorded(archive/'snapshot.db', request, package=archive/'k3_support',
                        site_packages=archive/manifest['dependency_directory'],
                        expected=manifest['expected'], timeout=remaining)


def catalog(root, *, limit=50):
    """Bounded metadata discovery, not artifact verification or a trust registry."""
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts or root == Path('/'):
        raise ValueError('absolute non-root catalog required')
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('catalog limit must be between 1 and 200')
    parent, name = _parent(root)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    items, truncated = [], False
    try:
        with os.scandir(fd) as entries:
            for scanned, entry in enumerate(entries):
                if scanned >= 1000 or len(items) >= limit:
                    truncated = True
                    break
                if not entry.is_dir(follow_symlinks=False):
                    continue
                item = {'name': entry.name, 'status': 'unreadable_or_invalid',
                        'trusted': False, 'replay_ready': False}
                try:
                    manifest = _read_json(root/entry.name/'manifest.json', 65536)
                    expected = manifest['expected']
                    if (manifest['schema'] != 'k3-replay-archive-v1'
                            or not isinstance(expected, dict)
                            or set(expected) != {'runtime', 'snapshot', 'request'}
                            or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v)
                                   for v in expected.values())):
                        raise ValueError('invalid metadata')
                    recorded_at = datetime.fromisoformat(manifest['recorded_at'])
                    if recorded_at.tzinfo is None:
                        raise ValueError('timezone required')
                    item.update(status='manifest_present_unverified',
                                recorded_at=recorded_at.isoformat(),
                                runtime_digest=expected['runtime'])
                except FileNotFoundError:
                    item['status'] = 'incomplete'
                except (OSError, ValueError, TypeError, KeyError):
                    pass
                items.append(item)
    finally:
        os.close(fd)
    return {'items': sorted(items, key=lambda item: item['name']),
            'truncated': truncated, 'read_only': True, 'content_read': False,
            'artifact_verification_performed': False}
