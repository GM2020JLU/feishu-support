"""Explicit artifact-bound replay; never infer historical capture provenance.

Runtime paths and expected digests are trusted operator inputs, not scenario or
model fields. No downloads, package installation, activation or implicit fallback.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from .ids import digest
from .recovery_inventory import _hash
from .replay_history import _payload, _snapshot_data, _run_snapshot_data, _validate_timeout


def _tree(root, deadline):
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts or root.is_symlink() or not root.is_dir():
        raise ValueError('explicit real runtime directory required')
    files, total = {}, 0
    def walk_error(error):
        raise error
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        if any((Path(directory)/name).is_symlink() for name in dirs):
            raise ValueError('runtime directory symlink rejected')
        for name in names:
            if time.monotonic() >= deadline:
                raise TimeoutError('runtime inventory deadline exceeded')
            if len(files) >= 50000:
                raise ValueError('runtime file limit exceeded')
            path = Path(directory)/name
            item = _hash(path, 1024**3-total)
            total += item['bytes']
            files[path.relative_to(root).as_posix()] = item
    if not files:
        raise ValueError('empty runtime directory')
    return digest(files)


def runtime_identity(*, package, site_packages, deadline):
    """Bind selected code/deps and this interpreter; caller must run the intended Python.

    System libraries/services/model weights are excluded, so this is not a full
    historical machine image. Includes every file, including data and bytecode.
    """
    package, site_packages = Path(package), Path(site_packages)
    if package.name != 'k3_support' or not (package/'__init__.py').is_file():
        raise ValueError('recorded k3_support package required')
    if site_packages.name not in ('site-packages', 'dist-packages'):
        raise ValueError('recorded dependency directory required')
    return {'schema': 'k3-recorded-runtime-v1',
            'package_digest': _tree(package, deadline),
            'dependencies_digest': _tree(site_packages, deadline),
            'python': sys.version,
            'interpreter': _hash(Path(sys.executable).resolve(), 128*1024**2),
            'excluded': ['system_libraries', 'stdlib_contents', 'external_services', 'model_weights']}


def run_recorded(database, request, *, package, site_packages, expected, timeout=30):
    """Execute a supplied proposal using exact recorded inputs in the existing sandbox.

    This does not certify when artifacts were captured or invoke a real model.
    Old packages must implement execute_snapshot; missing APIs fail, never adapt
    silently to current code. Hash rechecks are not protection from a hostile
    same-UID writer; operators must quiesce/runtime-protect the selected trees.
    """
    _validate_timeout(timeout)
    if (not isinstance(expected, dict) or set(expected) != {'runtime', 'snapshot', 'request'}
            or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for v in expected.values())):
        raise ValueError('three explicit recorded artifact digests required')
    expected = dict(expected)
    request = json.loads(_payload(request))
    if not isinstance(request, dict) or set(request) != {'config', 'event', 'proposal'}:
        raise ValueError('recorded replay requires exact config/event/proposal without overrides')
    if digest(request) != expected['request']:
        raise ValueError('recorded request differs')
    deadline = time.monotonic()+timeout
    identity = runtime_identity(package=package, site_packages=site_packages, deadline=deadline)
    if digest(identity) != expected['runtime']:
        raise ValueError('recorded runtime differs')
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise TimeoutError('recorded replay deadline exceeded')
    data = _snapshot_data(Path(database), upgrade_schema=False, timeout_seconds=remaining)
    if hashlib.sha256(data).hexdigest() != expected['snapshot']:
        raise ValueError('recorded snapshot differs')
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise TimeoutError('recorded replay deadline exceeded')
    result = _run_snapshot_data(data, request, timeout=remaining,
                               package=package, site_packages=site_packages)
    if runtime_identity(package=package, site_packages=site_packages, deadline=deadline) != identity:
        raise ValueError('runtime changed during recorded replay')
    if time.monotonic() >= deadline:
        raise TimeoutError('recorded replay deadline exceeded')
    return {'scope': 'explicit_recorded_artifacts_not_historical_capture_certification',
            'binding': expected, 'runtime': identity, 'result': result,
            'model_invoked': False, 'capture_time_verified': False, 'release_authorized': False}
