from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID, queued
from test_review import active_config

from k3_support.audit_inventory import page as audit
from k3_support.broker_claim import claim_next
from k3_support.broker_recovery import apply, preview, requeue_input
from k3_support.execution_inventory import page


def quarantined(conn, config):
    queued(conn)
    original = conn.execute("SELECT payload_json FROM broker_inputs").fetchone()[0]
    conn.execute("UPDATE broker_inputs SET payload_json='{}'")
    assert claim_next(conn, active_config(config), worker_uid=UID, now=NOW) is None
    row = conn.execute("SELECT * FROM jobs").fetchone()
    binding = {"job_id": row["job_id"], "expected_attempt": row["attempt_no"],
               "expected_lifecycle": row["lifecycle_round"], "expected_digest": row["input_digest"],
               "expected_updated_at": row["updated_at"], "now": NOW}
    return original, binding


def test_recovery_requires_repaired_input_and_grants_nothing(conn, config):
    original, binding = quarantined(conn, config)
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        requeue_input(conn, **binding)
    assert list(conn.iterdump()) == before
    conn.execute("UPDATE broker_inputs SET payload_json=?", (original,))
    assert requeue_input(conn, **binding)["execution_authorized"] is False
    assert tuple(conn.execute("SELECT state,attempt_no,error_class FROM jobs").fetchone()) == ("queued", 0, None)
    for table in ("broker_grants", "broker_execution_starts", "outbox"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    with pytest.raises(ValueError):
        requeue_input(conn, **binding)
    # Recovery is not execution approval: observation mode still cannot claim.
    assert claim_next(conn, config, worker_uid=UID, now=NOW) is None


@pytest.mark.parametrize("change", [
    "UPDATE jobs SET attempt_no=1", "UPDATE cases SET lifecycle_round=2",
    "UPDATE jobs SET updated_at='newer'", "UPDATE jobs SET state='cancelled'",
    "UPDATE jobs SET input_digest='changed'", "UPDATE jobs SET lease_owner='someone'",
])
def test_stale_recovery_is_read_only(conn, config, change):
    original, binding = quarantined(conn, config)
    conn.execute("UPDATE broker_inputs SET payload_json=?", (original,))
    conn.execute(change)
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        requeue_input(conn, **binding)
    assert list(conn.iterdump()) == before


def repaired(conn, config):
    original, binding = quarantined(conn, config)
    conn.execute("UPDATE broker_inputs SET payload_json=?", (original,))
    return binding["job_id"]


def test_retirement_after_preview_blocks_recovery_and_input_projection(conn, config):
    import sqlite3
    from k3_support.broker_input import project
    from k3_support.content_retirement import ContentRetiredError
    job_id = repaired(conn, config)
    shown = preview(conn, job_id=job_id)
    job = conn.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (job['case_id'], job['lifecycle_round'], 'recovery-retired', 'a'*64, 'b'*64, 'now', 'fixture'))
    before = conn.serialize()
    with pytest.raises(ValueError):
        apply(conn, job_id=job_id, binding_digest=shown['binding_digest'], request_id=str(uuid4()), actor_id='fixture')
    assert conn.serialize() == before
    def no_input(action, table, column, *_):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'broker_inputs' else sqlite3.SQLITE_OK
    conn.execute('SAVEPOINT retired_projection')
    conn.set_authorizer(no_input)
    try:
        with pytest.raises(ContentRetiredError):
            project(conn, job_id=job_id, case_id=job['case_id'], lifecycle_round=job['lifecycle_round'],
                    input_digest=job['input_digest'], request_id=str(uuid4()))
    finally:
        conn.set_authorizer(None)
        conn.execute('RELEASE retired_projection')


def test_preview_apply_is_audited_idempotent_and_no_authority(conn, config):
    job = repaired(conn, config)
    before = list(conn.iterdump())
    assert page(conn, config)["items"][0]["input_recovery_available"]
    shown = preview(conn, job_id=job)
    assert list(conn.iterdump()) == before
    args = {"job_id": job, "binding_digest": shown["binding_digest"],
            "request_id": str(uuid4()), "actor_id": "operator"}
    assert apply(conn, **args) == {"accepted": True, "replayed": False, "execution_authorized": False}
    # A delayed duplicate acknowledges the original action, not the current state.
    conn.execute("UPDATE jobs SET state='cancelled'")
    before = list(conn.iterdump())
    assert apply(conn, **args)["replayed"]
    assert list(conn.iterdump()) == before
    with pytest.raises(ValueError):
        apply(conn, **{**args, "actor_id": "different"})
    assert audit(conn, kind="execution")["items"][0]["actor_id"] == "operator"
    for table in ("broker_grants", "broker_execution_starts", "outbox"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["UPDATE cases SET version=version+1",
                                     "UPDATE jobs SET attempt_no=attempt_no+1",
                                     "UPDATE broker_inputs SET payload_json='{}'"])
def test_apply_revalidates_preview_atomically(conn, config, change):
    job = repaired(conn, config)
    shown = preview(conn, job_id=job)
    conn.execute(change)
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        apply(conn, job_id=job, binding_digest=shown["binding_digest"],
              request_id=str(uuid4()), actor_id="operator")
    assert list(conn.iterdump()) == before


def test_audit_failure_rolls_back_requeue(conn, config):
    job = repaired(conn, config)
    shown = preview(conn, job_id=job)
    conn.execute("""CREATE TEMP TRIGGER reject_recovery BEFORE INSERT ON broker_recovery_actions
                 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
    import sqlite3

    before = list(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError):
        apply(conn, job_id=job, binding_digest=shown["binding_digest"],
              request_id=str(uuid4()), actor_id="operator")
    assert list(conn.iterdump()) == before
