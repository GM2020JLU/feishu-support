from uuid import uuid4

from k3_support import notification_digest as batches
from k3_support.delivery import DeliveryReceipt, claim_outbox, deliver_claimed
from k3_support.notification_snooze import set_snooze
from k3_support.runtime_control import outbox_eligible
from k3_support.services import _outbox_tick
from k3_support.store import create_case, enqueue_outbox


def notices(conn, config, count=30):
    config.raw["mode"] = "active"
    set_snooze(
        conn, minutes=60, expected_revision=0, request_id=str(uuid4()), actor_id="owner"
    )
    ids = []
    for index in range(count):
        case, _ = create_case(
            conn, title="synthetic", case_type="bug", severity="P2", confidence=1
        )
        identifier, _ = enqueue_outbox(
            conn,
            channel="telegram",
            action_type="owner_decision",
            destination="telegram:owner-chat",
            payload={"text": "synthetic"},
            case_id=case,
            idempotency_key=f"synthetic-{index}",
        )
        ids.append((identifier, case))
    return ids


def resume(conn):
    set_snooze(
        conn, minutes=0, expected_revision=1, request_id=str(uuid4()), actor_id="owner"
    )


def test_thirty_deferred_notices_become_one_real_worker_delivery(conn, config):
    ids = notices(conn, config)
    assert batches.prepare(conn, config) is None
    resume(conn)
    before = [
        tuple(row) for row in conn.execute("SELECT * FROM cases ORDER BY case_id")
    ]
    sent = []

    def transport(destination, text):
        sent.append((destination, text))
        return DeliveryReceipt("synthetic-summary-receipt", {})

    def delivery(connection, cfg, row, **kwargs):
        kwargs["telegram_runner"] = transport
        return deliver_claimed(connection, cfg, row, **kwargs)

    result = _outbox_tick(conn, config, delivery=delivery)
    assert result["delivered"] == 1 and len(sent) == 1
    assert "30 条提醒" in sent[0][1] and "30 个事项" in sent[0][1]
    assert "任务尚未处理" in sent[0][1]
    assert conn.execute(
        "SELECT count(*) FROM outbox WHERE action_type='owner_decision' AND state='cancelled'"
    ).fetchone()[0] == len(ids)
    assert (
        conn.execute(
            "SELECT count(*) FROM notification_digest_members WHERE active=1"
        ).fetchone()[0]
        == 0
    )
    assert [
        tuple(row) for row in conn.execute("SELECT * FROM cases ORDER BY case_id")
    ] == before
    assert _outbox_tick(conn, config, delivery=delivery)["delivered"] == 0
    assert len(sent) == 1


def test_unknown_summary_retains_originals_and_never_rebatches(conn, config):
    notices(conn, config, 2)
    resume(conn)
    calls = []

    def timeout(*_args):
        calls.append(1)
        raise TimeoutError("synthetic uncertainty")

    def delivery(connection, cfg, row, **kwargs):
        kwargs["telegram_runner"] = timeout
        return deliver_claimed(connection, cfg, row, **kwargs)

    assert _outbox_tick(conn, config, delivery=delivery)["delivered"] == 0
    assert calls == [1]
    assert (
        conn.execute(
            "SELECT count(*) FROM outbox WHERE action_type='owner_decision' AND state='pending'"
        ).fetchone()[0]
        == 2
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM notification_digest_members WHERE active=1"
        ).fetchone()[0]
        == 2
    )
    assert _outbox_tick(conn, config, delivery=delivery)["delivered"] == 0
    assert calls == [1]
    assert (
        conn.execute("SELECT count(*) FROM notification_digest_batches").fetchone()[0]
        == 1
    )


def test_escalation_bypasses_digest_hold_and_invalidates_old_summary(conn, config):
    ids = notices(conn, config, 2)
    resume(conn)
    identifier = batches.prepare(conn, config)
    conn.execute("UPDATE cases SET severity='P1' WHERE case_id=?", (ids[0][1],))
    summary = dict(
        conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (identifier,)).fetchone()
    )
    assert not batches.valid(conn, summary)
    claimed = claim_outbox(
        conn, worker_id="test", eligible=lambda row: outbox_eligible(conn, config, row)
    )
    assert claimed["outbox_id"] == ids[0][0]


def test_exact_summary_text_and_destination_are_bound(conn, config):
    notices(conn, config, 2)
    resume(conn)
    identifier = batches.prepare(conn, config)
    summary = dict(
        conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (identifier,)).fetchone()
    )
    assert batches.valid(conn, summary)
    assert not batches.valid(
        conn, {**summary, "destination": "telegram:another-person"}
    )
    assert not batches.valid(
        conn, {**summary, "payload_json": '{"text":"changed","notice_count":2}'}
    )


def test_escalation_during_send_preserves_urgent_original_after_receipt(conn, config):
    ids = notices(conn, config, 2)
    resume(conn)

    def transport(*_args):
        conn.execute("UPDATE cases SET severity='P1' WHERE case_id=?", (ids[0][1],))
        return DeliveryReceipt("synthetic-receipt", {})

    def delivery(connection, cfg, row, **kwargs):
        kwargs["telegram_runner"] = transport
        return deliver_claimed(connection, cfg, row, **kwargs)

    assert _outbox_tick(conn, config, delivery=delivery)["delivered"] == 1
    states = dict(
        conn.execute(
            "SELECT outbox_id,state FROM outbox WHERE action_type='owner_decision'"
        )
    )
    assert states[ids[0][0]] == "pending"
    assert states[ids[1][0]] == "cancelled"
    claimed = claim_outbox(
        conn, worker_id="test", eligible=lambda row: outbox_eligible(conn, config, row)
    )
    assert claimed["outbox_id"] == ids[0][0]


def test_unsent_cancelled_digest_can_regroup_without_losing_originals(conn, config):
    notices(conn, config, 2)
    resume(conn)
    first = batches.prepare(conn, config)
    conn.execute("UPDATE outbox SET state='cancelled' WHERE outbox_id=?", (first,))
    second = batches.prepare(conn, config)
    assert first != second
    assert (
        conn.execute(
            "SELECT count(*) FROM notification_digest_members WHERE digest_outbox_id=? AND active=0",
            (first,),
        ).fetchone()[0]
        == 2
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM notification_digest_members WHERE digest_outbox_id=? AND active=1",
            (second,),
        ).fetchone()[0]
        == 2
    )
