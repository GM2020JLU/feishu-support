import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import yaml

from k3_support import cli
from k3_support import notification_snooze as snooze
from k3_support.delivery import DeliveryError, claim_outbox, deliver_claimed
from k3_support.runtime_control import outbox_eligible
from k3_support.store import create_case, enqueue_outbox


def set_quiet(conn, minutes=60, revision=0, **kwargs):
    return snooze.set_snooze(
        conn,
        minutes=minutes,
        expected_revision=revision,
        request_id=str(uuid4()),
        actor_id="owner-user",
        **kwargs,
    )


def test_snooze_expiry_replay_and_stale_requests(conn):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    request = str(uuid4())
    args = {
        "minutes": 60,
        "expected_revision": 0,
        "request_id": request,
        "actor_id": "owner",
    }
    first = snooze.set_snooze(conn, now=now, **args)
    replay = snooze.set_snooze(conn, now=now + timedelta(minutes=10), **args)
    assert replay["replayed"] and replay["until_at"] == first["until_at"]
    assert snooze.status(conn, now=now + timedelta(minutes=59))["active"]
    assert not snooze.status(conn, now=now + timedelta(minutes=60))["active"]
    with pytest.raises(ValueError, match="同一请求号"):
        snooze.set_snooze(conn, now=now, **{**args, "minutes": 30})
    with pytest.raises(ValueError, match="已变化"):
        set_quiet(conn)
    set_quiet(conn, minutes=0, revision=1)
    assert snooze.status(conn)["until_at"] is None


def test_ordinary_notices_defer_but_urgent_and_other_actions_do_not(conn, config):
    config.raw["mode"] = "active"
    set_quiet(conn)
    for severity in ("P0", "P1", "P2", "P3"):
        case, _ = create_case(
            conn, title="synthetic", case_type="bug", severity=severity, confidence=1
        )
        row = {"case_id": case, "channel": "telegram", "action_type": "owner_decision"}
        assert outbox_eligible(conn, config, row) is (severity in {"P0", "P1"})
        for action in (
            "incident_alert",
            "p0_alert",
            "approval_request",
            "mail_summary",
        ):
            assert outbox_eligible(conn, config, {**row, "action_type": action})
    assert not snooze.deferred(
        conn, {"case_id": None, "channel": "telegram", "action_type": "owner_decision"}
    )


def test_queue_retained_and_snooze_after_claim_blocks_actual_transport(conn, config):
    config.raw["mode"] = "active"
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
        idempotency_key="synthetic-snooze",
    )
    claimed = claim_outbox(
        conn, worker_id="test", eligible=lambda row: outbox_eligible(conn, config, row)
    )
    assert claimed is not None
    original = dict(
        conn.execute("SELECT * FROM cases WHERE case_id=?", (case,)).fetchone()
    )
    set_quiet(conn)
    calls = []
    with pytest.raises(DeliveryError, match="disabled"):
        deliver_claimed(
            conn, config, claimed, telegram_runner=lambda *args: calls.append(args)
        )
    assert calls == []
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE outbox_id=?", (identifier,)
        ).fetchone()[0]
        == "pending"
    )
    assert (
        claim_outbox(
            conn,
            worker_id="test",
            eligible=lambda row: outbox_eligible(conn, config, row),
        )
        is None
    )
    assert (
        dict(conn.execute("SELECT * FROM cases WHERE case_id=?", (case,)).fetchone())
        == original
    )
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    set_quiet(conn, minutes=0, revision=1)
    from k3_support.notification_digest import prepare

    summary = prepare(conn, config)
    assert (
        claim_outbox(
            conn,
            worker_id="test",
            eligible=lambda row: outbox_eligible(conn, config, row),
        )["outbox_id"]
        == summary
    )


def test_cli_auth_precedes_writes_and_read_does_not_mutate(conn, config, capsys):
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    common = ["--config", str(config.path)]
    before = conn.serialize()
    assert cli.main([*common, "notification-status"]) == 0
    assert json.loads(capsys.readouterr().out)["read_only"]
    assert conn.serialize() == before
    args = [
        *common,
        "notification-snooze",
        "--minutes",
        "60",
        "--expected-revision",
        "0",
        "--request-id",
        str(uuid4()),
        "--control-user-id",
        "forged",
        "--control-chat-id",
        "owner-chat",
    ]
    assert cli.main(args) == 2
    capsys.readouterr()
    assert conn.serialize() == before
    args[args.index("forged")] = "owner-user"
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 1


@pytest.mark.parametrize("minutes", [-1, 1441, True, 1.5])
def test_invalid_duration_never_writes(conn, minutes):
    before = conn.serialize()
    with pytest.raises(ValueError):
        set_quiet(conn, minutes=minutes)
    assert conn.serialize() == before


def test_concurrent_snooze_and_resume_have_one_revision_winner(conn, config):
    from k3_support.db import connect

    barrier = threading.Barrier(2)

    def contender(minutes):
        local = connect(config.database_path)
        try:
            barrier.wait(timeout=5)
            try:
                return set_quiet(local, minutes=minutes)["revision"]
            except ValueError:
                return 0
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(contender, (60, 0))) == [0, 1]
    assert (
        conn.execute("SELECT count(*) FROM notification_snooze_history").fetchone()[0]
        == 1
    )
