import hashlib
import json
from datetime import UTC, datetime

import pytest

from k3_support.broker_task_binding import BindingError, verify_live_task
from k3_support.job_capability import CapabilityError
from k3_support.store import create_case


def seed(conn):
    case_id, _ = create_case(conn, title="synthetic", case_type="bug", severity="P3", confidence=0.8)
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (case_id,))
    conn.execute("""INSERT INTO jobs(job_id,case_id,job_type,state,lease_owner,lease_expires_at,
                    input_digest,attempt_no,available_at,created_at,updated_at,context_json)
                    VALUES('job-1',?,'codex','running','worker','2026-10-01T00:00:00+00:00',?,1,'now','now','now',?)""",
                 (case_id, "a" * 64, json.dumps({"capability_sha256": hashlib.sha256(b"secret").hexdigest()})))
    return {"job_id": "job-1", "case_id": case_id, "execution_round": 1, "lifecycle_round": 1,
            "input_digest": "a" * 64, "lease_token": "secret"}


def test_live_binding_is_read_only(conn):
    params = seed(conn)
    before = list(conn.iterdump())
    result = verify_live_task(conn, params, now=datetime(2026, 9, 8, tzinfo=UTC))
    assert result["attempt_no"] == 1
    assert "secret" not in str(result)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("change", ["attempt_no=2", "lifecycle_round=2", "input_digest='other'", "state='cancelled'",
                                    "lease_owner=NULL", "lease_expires_at=NULL", "lease_expires_at='2026-01-01T00:00:00+00:00'",
                                    "lease_expires_at='2027-01-01T00:00:00'"])
def test_stale_authority_rejected(conn, change):
    params = seed(conn)
    conn.execute(f"UPDATE jobs SET {change} WHERE job_id='job-1'")
    with pytest.raises(BindingError):
        verify_live_task(conn, params, now=datetime(2026, 9, 8, tzinfo=UTC))


@pytest.mark.parametrize("change", ["lifecycle_round=2", "state='paused'", "state='resolved'", "state='cancelled'", "state='takeover'"])
def test_case_revocation_blocks_old_task(conn, change):
    params = seed(conn)
    conn.execute(f"UPDATE cases SET {change} WHERE case_id=?", (params["case_id"],))
    before = list(conn.iterdump())
    with pytest.raises(BindingError):
        verify_live_task(conn, params, now=datetime(2026, 9, 8, tzinfo=UTC))
    assert list(conn.iterdump()) == before


def test_wrong_secret_and_cross_case_binding_rejected(conn):
    params = seed(conn)
    with pytest.raises(CapabilityError):
        verify_live_task(conn, {**params, "lease_token": "forged"}, now=datetime(2026, 9, 8, tzinfo=UTC))
    with pytest.raises(BindingError):
        verify_live_task(conn, {**params, "case_id": "other"}, now=datetime(2026, 9, 8, tzinfo=UTC))


def test_lease_boundary_is_exclusive(conn):
    params = seed(conn)
    with pytest.raises(BindingError, match="expired"):
        verify_live_task(conn, params, now=datetime(2026, 10, 1, tzinfo=UTC))
