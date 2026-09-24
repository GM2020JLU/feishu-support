from dataclasses import FrozenInstanceError

import pytest
from test_mail_calendar_base import configured

from k3_support.base_sync import enqueue_dirty_entities
from k3_support.base_sync_attempt import AttemptRef
from k3_support.db import transaction
from k3_support.store import claim_jobs, create_case


def claimed(conn, config):
    create_case(conn, title='synthetic', case_type='bug', severity='P3', confidence=0.8)
    enqueue_dirty_entities(conn, configured(config, base_sync=True))
    return claim_jobs(conn, 'original-worker', job_types=('base_sync',))[0]


@pytest.mark.parametrize('change', [
    "UPDATE jobs SET attempt_no=attempt_no+1",
    "UPDATE jobs SET lease_owner='successor'",
    "UPDATE jobs SET input_digest=printf('%064d',1)",
    "UPDATE jobs SET lifecycle_round=lifecycle_round+1",
    "UPDATE jobs SET state='queued'",
    "UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00+00:00'",
    "UPDATE jobs SET lease_expires_at='2099-01-01T00:00:00'",
])
def test_claim_ref_never_adopts_new_identity_or_expired_lease(conn, config, change):
    ref = AttemptRef.from_claim(claimed(conn, config))
    with transaction(conn):
        assert ref.current(conn) is not None
    conn.execute(change)
    with transaction(conn):
        assert ref.current(conn) is None


def test_identity_is_frozen_and_check_requires_transaction(conn, config):
    ref = AttemptRef.from_claim(claimed(conn, config))
    with pytest.raises(FrozenInstanceError):
        ref.owner = 'successor'
    with pytest.raises(ValueError, match='transaction'):
        ref.current(conn)
