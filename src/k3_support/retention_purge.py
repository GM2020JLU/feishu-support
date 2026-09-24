"""Preflight for irreversible quarantine retirement; no deletion authorization."""
import json
import os
import stat
from datetime import timedelta

from .ids import digest
from .db import transaction
from .operations import _artifact_stamp, _retention_referenced, _safe_artifact
from .timeutil import iso_now, parse_iso, utc_now


def preview(conn, config, *, attempt_id, days):
    if not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 100:
        raise ValueError('invalid quarantine attempt')
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('quarantine retention must be 1 to 3650 days')
    row = conn.execute('SELECT * FROM retention_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
    if row is None:
        raise ValueError('quarantine attempt not found')
    original = _safe_artifact(config, row['original_path'])
    path = _safe_artifact(config, row['quarantine_path'])
    if path != original.parent / ('.retention-' + attempt_id) / 'payload':
        raise ValueError('quarantine layout does not match attempt')
    source = conn.execute('SELECT raw_artifact_path FROM inbound_events WHERE event_pk=?', (row['event_pk'],)).fetchone()
    blockers = []
    if row['state'] != 'quarantined':
        blockers.append('not_quarantined')
    if parse_iso(row['updated_at']) >= utc_now() - timedelta(days=days):
        blockers.append('quarantine_grace_period')
    if source is None or source[0] != str(path):
        blockers.append('source_binding_changed')
    stamp = _artifact_stamp(path)
    if stamp is None or stamp[:4] != json.loads(row['file_stamp_json'])[:4]:
        blockers.append('file_missing_or_changed')
    if original.exists():
        blockers.append('original_path_occupied')
    if (_retention_referenced(conn, row['event_pk'], path)
            or _retention_referenced(conn, row['event_pk'], original)):
        blockers.append('referenced')
    if conn.execute("SELECT 1 FROM retention_recovery_requests WHERE attempt_id=? AND state IN ('running','unknown')",
                    (attempt_id,)).fetchone():
        blockers.append('recovery_unresolved')
    if conn.execute("SELECT 1 FROM retention_purge_requests WHERE attempt_id=? AND state IN ('prepared','running','unknown','purged')",
                    (attempt_id,)).fetchone():
        blockers.append('purge_request_exists')
    return {'attempt_id': attempt_id, 'days': days, 'blockers': blockers,
            'binding_digest': digest({'attempt': dict(row), 'file_stamp': stamp,
                                      'source_path': source[0] if source else None, 'days': days}),
            'logical_bytes': stamp[2] if stamp else 0, 'file_stamp': stamp, 'read_only': True,
            'deletion_authorized': False,
            'scope': 'known_reference_preflight_not_secure_erasure_or_backup_cleanup'}


def prepare(conn, config, *, attempt_id, days, binding_digest, request_id, actor_id,
            confirm_permanent_delete=False):
    """Persist an explicit intent; preparation does not unlink or clear paths."""
    from uuid import UUID
    if (not isinstance(request_id, str) or str(UUID(request_id)) != request_id
            or not isinstance(actor_id, str) or not 1 <= len(actor_id.strip()) <= 256
            or type(days) is not int or not 1 <= days <= 3650
            or confirm_permanent_delete is not True):
        raise ValueError('explicit purge confirmation and valid actor/request required')
    with transaction(conn):
        prior = conn.execute('SELECT * FROM retention_purge_requests WHERE request_id=?', (request_id,)).fetchone()
        if prior:
            if (prior['attempt_id'], prior['days'], prior['binding_digest'], prior['actor_id']) != (
                    attempt_id, days, binding_digest, actor_id):
                raise ValueError('purge request binding changed')
            return {'request_id': request_id, 'state': prior['state'], 'replayed': True, 'files_deleted': 0}
        current = preview(conn, config, attempt_id=attempt_id, days=days)
        if current['blockers'] or current['binding_digest'] != binding_digest:
            raise ValueError('purge preview blocked or stale')
        now = iso_now()
        conn.execute("INSERT INTO retention_purge_requests VALUES(?,?,?,?,?,'prepared',?,?)",
                     (request_id, attempt_id, actor_id, binding_digest, days, now, now))
    return {'request_id': request_id, 'state': 'prepared', 'replayed': False, 'files_deleted': 0}


def execute(conn, config, *, request_id, actor_id):
    """Consume one confirmed intent. Unknown attempts are never retried here."""
    from .retention_operation_guard import hold
    with hold(config):
        return _execute_locked(conn, config, request_id=request_id, actor_id=actor_id)


def _execute_locked(conn, config, *, request_id, actor_id):
    from .retention_recovery import _parent
    with transaction(conn):
        request = conn.execute('SELECT * FROM retention_purge_requests WHERE request_id=?', (request_id,)).fetchone()
        if request is None or request['actor_id'] != actor_id:
            raise ValueError('purge request unavailable for actor')
        if request['state'] != 'prepared':
            return {'request_id': request_id, 'state': request['state'], 'replayed': True, 'files_deleted': 0}
        _recheck(conn, config, request)
        conn.execute("UPDATE retention_purge_requests SET state='running',updated_at=? WHERE request_id=?",
                     (iso_now(), request_id))
    try:
        with transaction(conn):
            current = conn.execute('SELECT * FROM retention_purge_requests WHERE request_id=?', (request_id,)).fetchone()
            if current['state'] != 'running':
                raise ValueError('purge state changed')
            checked = _recheck(conn, config, current)
            attempt = conn.execute('SELECT * FROM retention_attempts WHERE attempt_id=?', (current['attempt_id'],)).fetchone()
            path = _safe_artifact(config, attempt['quarantine_path'])
            expected = checked['file_stamp']
            parent, name = _parent(path)
            try:
                observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
                stamp = [observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns]
                if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or stamp != expected:
                    raise ValueError('quarantine file changed before unlink')
                # Controller-owned directory and write transaction fence normal
                # recovery/reference writers. External same-UID mutation is not
                # an atomic filesystem/database transaction.
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
            conn.execute('UPDATE inbound_events SET raw_artifact_path=NULL WHERE event_pk=? AND raw_artifact_path=?',
                         (attempt['event_pk'], str(path)))
            conn.execute("UPDATE retention_attempts SET state='cancelled',reason='permanently_purged',updated_at=? WHERE attempt_id=?",
                         (iso_now(), attempt['attempt_id']))
            conn.execute("UPDATE retention_purge_requests SET state='purged',updated_at=? WHERE request_id=?", (iso_now(), request_id))
        return {'request_id': request_id, 'state': 'purged', 'replayed': False,
                'files_deleted': 1, 'logical_bytes': checked['logical_bytes']}
    except BaseException:
        with transaction(conn):
            conn.execute("UPDATE retention_purge_requests SET state='unknown',updated_at=? WHERE request_id=? AND state='running'",
                         (iso_now(), request_id))
        raise


def _recheck(conn, config, request):
    if parse_iso(request['created_at']) + timedelta(minutes=10) <= utc_now():
        raise ValueError('purge confirmation expired; cancel and prepare again')
    checked = preview(conn, config, attempt_id=request['attempt_id'], days=request['days'])
    blockers = [item for item in checked['blockers'] if item != 'purge_request_exists']
    if blockers or checked['binding_digest'] != request['binding_digest']:
        raise ValueError('purge intent is blocked or stale')
    return checked


def cancel(conn, *, request_id, actor_id):
    """Only an unstarted intent can be safely cancelled without file I/O."""
    with transaction(conn):
        request = conn.execute('SELECT * FROM retention_purge_requests WHERE request_id=?', (request_id,)).fetchone()
        if request is None or request['actor_id'] != actor_id:
            raise ValueError('purge request unavailable for actor')
        if request['state'] not in ('prepared', 'cancelled'):
            raise ValueError('started purge requires reconciliation, not cancellation')
        replayed = request['state'] == 'cancelled'
        conn.execute("UPDATE retention_purge_requests SET state='cancelled',updated_at=? WHERE request_id=? AND state='prepared'",
                     (iso_now(), request_id))
    return {'request_id': request_id, 'state': 'cancelled', 'replayed': replayed, 'files_deleted': 0}


def inspect(conn, config, *, request_id, actor_id):
    """Read filesystem observations; absence does not certify who deleted it."""
    request = conn.execute('SELECT * FROM retention_purge_requests WHERE request_id=?', (request_id,)).fetchone()
    if request is None or request['actor_id'] != actor_id:
        raise ValueError('purge request unavailable for actor')
    attempt = conn.execute('SELECT * FROM retention_attempts WHERE attempt_id=?', (request['attempt_id'],)).fetchone()
    source = conn.execute('SELECT raw_artifact_path FROM inbound_events WHERE event_pk=?', (attempt['event_pk'],)).fetchone()
    observations = {}
    for field in ('original_path', 'quarantine_path'):
        try:
            path = _safe_artifact(config, attempt[field])
            stamp = _artifact_stamp(path)
            observations[field] = {'state': 'present' if stamp else 'absent', 'stamp': stamp}
        except (OSError, ValueError, RuntimeError) as error:
            observations[field] = {'state': 'unavailable', 'error_class': type(error).__name__}
    value = {'request_id': request_id, 'request_state': request['state'],
             'attempt_state': attempt['state'], 'observations': observations,
             'source_binding': 'cleared' if source and source[0] is None else
             'quarantine' if source and source[0] == attempt['quarantine_path'] else 'other',
             'read_only': True, 'retry_authorized': False,
             'notice': 'File absence is an observation, not proof of successful purge; unknown attempts remain held.'}
    return {**value, 'observation_digest': digest({'view': value, 'request': dict(request),
        'attempt': dict(attempt), 'source_path': source[0] if source else None})}


def reconcile(conn, config, *, request_id, actor_id, decision, observation_digest, confirm=False):
    """Explicit state repair after executor exit. Never performs file deletion."""
    from .retention_operation_guard import hold
    if confirm is not True or decision not in ('keep_file', 'confirm_absence'):
        raise ValueError('explicit reconciliation decision required')
    with hold(config), transaction(conn):
        previous = conn.execute('SELECT * FROM retention_purge_reconciliations WHERE request_id=?', (request_id,)).fetchone()
        if previous:
            if (previous['actor_id'], previous['decision'], previous['observation_digest']) != (actor_id, decision, observation_digest):
                raise ValueError('reconciliation binding changed')
            return {'request_id': request_id, 'decision': decision, 'replayed': True, 'files_deleted': 0}
        current = inspect(conn, config, request_id=request_id, actor_id=actor_id)
        if current['request_state'] not in ('running', 'unknown') or current['observation_digest'] != observation_digest:
            raise ValueError('reconciliation observation stale or unnecessary')
        if current['source_binding'] != 'quarantine' or current['observations']['original_path']['state'] != 'absent':
            raise ValueError('source binding or original location requires manual investigation')
        request = conn.execute('SELECT * FROM retention_purge_requests WHERE request_id=?', (request_id,)).fetchone()
        attempt = conn.execute('SELECT * FROM retention_attempts WHERE attempt_id=?', (request['attempt_id'],)).fetchone()
        observed = current['observations']['quarantine_path']
        if decision == 'keep_file':
            if observed['state'] != 'present' or observed['stamp'][:4] != json.loads(attempt['file_stamp_json'])[:4]:
                raise ValueError('original quarantined file is not intact')
            state = 'cancelled'
        else:
            if observed['state'] != 'absent':
                raise ValueError('quarantine absence not observed')
            state = 'purged'
            conn.execute('UPDATE inbound_events SET raw_artifact_path=NULL WHERE event_pk=?', (attempt['event_pk'],))
            conn.execute("UPDATE retention_attempts SET state='cancelled',reason='operator_confirmed_absence',updated_at=? WHERE attempt_id=?",
                         (iso_now(), attempt['attempt_id']))
        conn.execute('UPDATE retention_purge_requests SET state=?,updated_at=? WHERE request_id=?', (state, iso_now(), request_id))
        conn.execute('INSERT INTO retention_purge_reconciliations VALUES(?,?,?,?,?)',
                     (request_id, actor_id, decision, observation_digest, iso_now()))
    return {'request_id': request_id, 'decision': decision, 'replayed': False, 'files_deleted': 0}
