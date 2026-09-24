"""Immutable Base worker identity captured from the original claim response."""

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Any

from .timeutil import iso_now


@dataclass(frozen=True)
class AttemptRef:
    job_id: str
    attempt_no: int
    owner: str
    input_digest: str
    lifecycle_round: int

    def __post_init__(self):
        if (any(not isinstance(value, str) or not value for value in
                (self.job_id, self.owner, self.input_digest))
                or len(self.input_digest) != 64
                or any(char not in '0123456789abcdef' for char in self.input_digest)
                or type(self.attempt_no) is not int or self.attempt_no < 1
                or type(self.lifecycle_round) is not int or self.lifecycle_round < 1):
            raise ValueError('invalid claimed Base attempt identity')

    @classmethod
    def from_claim(cls, claimed: Mapping[str, Any]):
        # Caller supplies the original claim, never a reread after transport.
        if claimed['job_type'] != 'base_sync' or claimed['state'] != 'running':
            raise ValueError('running Base claim required')
        return cls(claimed['job_id'], claimed['attempt_no'], claimed['lease_owner'],
                   claimed['input_digest'], claimed['lifecycle_round'])

    def current(self, conn, *, now=None):
        """Return the exact still-owned attempt within the caller's write lock."""
        if not conn.in_transaction:
            raise ValueError('Base attempt check requires control transaction')
        row = conn.execute('''SELECT * FROM jobs WHERE job_id=? AND job_type='base_sync'
            AND state='running' AND attempt_no=? AND lease_owner=? AND input_digest=?
            AND lifecycle_round=?''',
            (self.job_id, self.attempt_no, self.owner, self.input_digest, self.lifecycle_round)).fetchone()
        if row is None:
            return None
        try:
            expires = datetime.fromisoformat(row['lease_expires_at'])
            at = datetime.fromisoformat(now or iso_now())
            if expires.tzinfo is None or at.tzinfo is None or expires <= at:
                return None
        except (TypeError, ValueError):
            return None
        return row
