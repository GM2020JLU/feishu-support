"""Explicit read-only remote verification capture, not an automatic replay action."""
import argparse
import copy
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .codex_result_source import read_result
from .config import load_config
from .executors import run_process, validate_codex_result_text
from .ids import canonical_json
from .replay_snapshot import replay_snapshot
from .replay_transcript import VerificationTranscript
from .review import validate_codex_manifest, verify_codex_manifest


@contextmanager
def private_output(target):
    parent = target.parent
    if not target.is_absolute() or parent.resolve() != parent:
        raise ValueError('canonical absolute output path required')
    expected = parent.stat()
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = os.fstat(directory)
        if ((current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
                or current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) & 0o077):
            raise ValueError('output directory must be unchanged, owned and private')
        fd = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            yield output
        os.fsync(directory)
        after = parent.stat()
        if (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError('output directory moved during capture; inspect reserved file')
    finally:
        os.close(directory)


def capture(config, *, job_id, confirm_remote_verification, runner):
    if confirm_remote_verification is not True or not callable(runner):
        raise ValueError('explicit remote verification confirmation and runner required')
    records = []

    def record(argv, cwd, timeout):
        if len(records) >= 100:
            raise ValueError('capture command limit reached')
        arguments = copy.deepcopy(argv)
        result = runner(argv, cwd, timeout)
        row = dict(argv=arguments, cwd=cwd, timeout=timeout, returncode=result.returncode,
                   stdout=result.stdout, stderr=result.stderr)
        VerificationTranscript([*records, row])
        records.append(row)
        return result

    with replay_snapshot(config.database_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE job_id=? AND job_type='codex' AND state='succeeded'",
                           (job_id,)).fetchone()
        if job is None or not conn.execute('SELECT 1 FROM broker_grants WHERE job_id=?', (job_id,)).fetchone():
            raise ValueError('completed captured broker job required')
        sections = validate_codex_result_text(read_result(conn, job_id=job_id).decode())
        repositories = set(json.loads(job['context_json']).get('repositories') or [])
        if not repositories:
            raise ValueError('immutable repository context required')
        manifest = validate_codex_manifest(sections['artifacts'], case_id=job['case_id'],
            configured_repositories=repositories, remote_worktree_root=config.runtime('remote_worktree_root'))
        verify_codex_manifest(config, case_id=job['case_id'], manifest=manifest, runner=record)
    return {'job_id': job_id, 'transcript': records}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Capture current read-only remote checks for Debug replay; no model/board/send.')
    parser.add_argument('--config', required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--confirm-remote-verification', action='store_true')
    args = parser.parse_args(argv)
    if not args.confirm_remote_verification:
        parser.error('explicit --confirm-remote-verification required; this contacts the configured remote host')
    try:
        target = args.output
        config = load_config(args.config)
        # Reserve before contacting the remote. An existing file never triggers
        # another verification. Failure leaves the reserved file for inspection.
        with private_output(target) as output:
            result = capture(config, job_id=args.job_id, confirm_remote_verification=True, runner=run_process)
            payload = canonical_json(result)
            if len(payload.encode()) > 61440:
                raise ValueError('captured bundle exceeds GUI import limit')
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        print(json.dumps({'ok': True, 'records': len(result['transcript']), 'output': str(target),
                          'model_called': False, 'production_db_changed': False}))
        return 0
    except Exception as error:
        print(json.dumps({'ok': False, 'error_type': type(error).__name__,
                          'note': 'No automatic retry. A reserved output file may remain; do not treat it as a complete capture.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
