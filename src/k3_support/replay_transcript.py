"""Finite command-response fixtures. Never executes commands or reads paths."""
import copy

from .executors import ExecutionResult
from .ids import canonical_json, digest


class VerificationTranscript:
    def __init__(self, records):
        if not isinstance(records, list) or len(records) > 100:
            raise ValueError('verification transcript allows at most 100 records')
        for row in records:
            if not isinstance(row, dict) or set(row) != {'argv', 'cwd', 'timeout', 'returncode', 'stdout', 'stderr'}:
                raise ValueError('invalid verification transcript record')
            if (not isinstance(row['argv'], list) or not 1 <= len(row['argv']) <= 128
                    or any(not isinstance(arg, str) or len(arg) > 16384 or '\x00' in arg for arg in row['argv'])):
                raise ValueError('invalid transcript argv')
            if row['cwd'] is not None and (not isinstance(row['cwd'], str) or len(row['cwd']) > 4096):
                raise ValueError('invalid transcript cwd')
            if type(row['timeout']) is not int or not 1 <= row['timeout'] <= 3600:
                raise ValueError('invalid transcript timeout')
            if type(row['returncode']) is not int or not -255 <= row['returncode'] <= 255:
                raise ValueError('invalid transcript returncode')
            if any(not isinstance(row[key], str) or len(row[key]) > 200000 for key in ('stdout', 'stderr')):
                raise ValueError('invalid transcript output')
        if len(canonical_json(records).encode()) > 1048576:
            raise ValueError('verification transcript exceeds byte limit')
        self.records = copy.deepcopy(records)
        self.position = 0
        self.failed = False
        self.digest = digest(records)

    def __call__(self, argv, cwd, timeout):
        if self.failed or self.position >= len(self.records):
            self.failed = True
            raise ValueError('verification transcript exhausted or invalidated')
        row = self.records[self.position]
        if argv != row['argv'] or cwd != row['cwd'] or timeout != row['timeout']:
            self.failed = True
            raise ValueError('verification transcript does not match requested operation')
        self.position += 1
        return ExecutionResult(copy.deepcopy(row['argv']), row['returncode'], row['stdout'], row['stderr'])

    def status(self):
        return {'digest': self.digest, 'consumed': self.position,
                'remaining': len(self.records) - self.position, 'failed': self.failed,
                'scope': 'supplied_command_response_fixtures_not_live_verification'}
