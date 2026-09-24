"""Fixed-root, explicitly confirmed archive replay for the authenticated console."""
from pathlib import Path
import re
import threading
import sysconfig

from .ids import digest
from .replay_archive import capture
from .replay_history import _payload

from .replay_archive import replay

_RUNNING = threading.BoundedSemaphore(1)


def capture_current(conn, config, *, name, event, proposal, confirmed, writers_quiesced):
    """Archive a stopped instance, never arbitrary browser-selected paths.

    The operator's external-writer assertion is not independent attestation.
    The write reservation keeps database state fixed during this bounded copy.
    """
    if (not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', name)
            or confirmed is not True or writers_quiesced is not True):
        raise ValueError('archive name and explicit private-copy/quiescence confirmation required')
    if not isinstance(event, dict) or not isinstance(proposal, dict):
        raise ValueError('event and proposal must be JSON objects')
    request = {'config': config.raw, 'event': event, 'proposal': proposal}
    _payload(request)
    if conn.in_transaction:
        raise ValueError('archive requires an independent database transaction')
    if not _RUNNING.acquire(blocking=False):
        raise ValueError('an archive operation is already running')
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            state = conn.execute("SELECT mode FROM global_control_state WHERE scope='feishu_support'").fetchone()
            if not state or state['mode'] != 'stopped':
                raise ValueError('fully stop the instance before capture')
            root = config.data_dir/'replay-archives'
            root.mkdir(mode=0o700, exist_ok=True)
            manifest = capture(config.database_path, request,
                package=Path(__file__).resolve().parent,
                site_packages=Path(sysconfig.get_paths()['purelib']),
                output=root/name, timeout=30)
            return {'name': name, 'manifest_digest': digest(manifest),
                    'capture_time_verified': False, 'release_authorized': False,
                    'external_writers_independently_verified': False}
        finally:
            conn.rollback()
    finally:
        _RUNNING.release()


def run_selected(root, *, name, manifest_digest, confirmed):
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts or root == Path('/'):
        raise ValueError('invalid managed archive root')
    if (not isinstance(name, str) or not name or name in {'.','..'}
            or '/' in name or '\\' in name or '\x00' in name or len(name.encode()) > 255):
        raise ValueError('select one archive name, not a path')
    if confirmed is not True:
        raise ValueError('explicit isolated replay confirmation required')
    if not isinstance(manifest_digest, str) or not re.fullmatch('[a-f0-9]{64}',manifest_digest):
        raise ValueError('independently retained manifest digest required')
    if not _RUNNING.acquire(blocking=False):
        raise ValueError('an archive replay is already running; do not retry automatically')
    try:
        return replay(root/name,manifest_digest=manifest_digest,timeout=30)
    finally:
        _RUNNING.release()
