from datetime import UTC, datetime

from test_attention_races import race
from test_broker_receipts import setup

from k3_support.broker_receipts import execute


def test_concurrent_request_replay_commits_one_business_write(conn, config):
    request = setup(conn)

    def handler(db, binding, value):
        db.execute("UPDATE jobs SET priority=priority+1 WHERE job_id=?", (binding["job_id"],))
        return {"accepted": True, "job_id": binding["job_id"]}

    def submit(other):
        return execute(other, request, peer_uid=1234, handler=handler,
                       now=datetime(2026, 9, 8, 0, 30, tzinfo=UTC))

    initial = conn.execute("SELECT priority FROM jobs WHERE job_id='job-1'").fetchone()[0]
    left, right = race(config, submit, submit)
    assert left == right
    assert conn.execute("SELECT priority FROM jobs WHERE job_id='job-1'").fetchone()[0] == initial + 1
    assert conn.execute("SELECT count(*) FROM broker_receipts").fetchone()[0] == 1

    def forbidden(*args):
        raise AssertionError("committed request must not execute again")

    assert execute(conn, request, peer_uid=1234, handler=forbidden,
                   now=datetime(2026, 9, 8, 0, 31, tzinfo=UTC)) == left


def test_replay_returns_original_receipt_after_job_finishes(conn):
    request = setup(conn)

    def finish(db, binding, value):
        db.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (binding["job_id"],))
        return {"accepted": True, "job_id": binding["job_id"]}

    args = {"peer_uid": 1234, "now": datetime(2026, 9, 8, 0, 30, tzinfo=UTC)}
    first = execute(conn, request, handler=finish, **args)
    before = list(conn.iterdump())
    assert execute(conn, request, handler=lambda *args: None, **args) == first
    assert list(conn.iterdump()) == before
