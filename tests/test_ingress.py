from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest

from k3_support.config import Config, validate_config
from k3_support.delivery import claim_outbox, deliver_claimed
from k3_support.ingress import (
    IngressError,
    ingest_bot_value,
    normalize_polled_message,
    poll_user_mail,
    poll_user_messages,
)
from k3_support.lark import CommandResult, LarkError
from k3_support.orchestrator import claim_inbound
from k3_support.runtime_control import ensure_global_state, outbox_eligible
from k3_support.store import enqueue_outbox, ingest_event
from k3_support.timeutil import parse_iso


def ingress_config(config):
    raw = copy.deepcopy(config.raw)
    raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    raw["scope"]["technical_chat_ids"] = ["oc_tech"]
    return Config(validate_config(raw), config.path)


def test_native_feishu_bug_control_is_authenticated_and_replay_fenced(conn, config):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["identity"].update({
        "control_operator_id": "owner-operator",
        "feishu_control_user_id": "ou_control",
        "feishu_control_chat_id": "oc_control",
    })
    cfg = Config(validate_config(raw), config.path)
    event = {
        "message_id": "om_native_control_1",
        "chat_id": "oc_control",
        "chat_type": "p2p",
        "sender_id": "ou_control",
        "create_time": "1788220800000",
        "message_type": "text",
        "content": "bug list",
    }
    assert ingest_bot_value(conn, cfg, event) == (None, False)
    row = conn.execute("SELECT action_type,destination,payload_json FROM outbox").fetchone()
    assert (row["action_type"], row["destination"]) == ("control_receipt", "oc_control")
    assert "已绑定的 Bug" in row["payload_json"]
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0


    assert ingest_bot_value(conn, cfg, event) == (None, False)
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    with pytest.raises(IngressError, match="content changed"):
        ingest_bot_value(conn, cfg, {**event, "content": "bug create-dispatch other"})
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1

    other = {**event, "message_id": "om_native_control_2", "sender_id": "ou_other"}
    foreign_pk, admitted = ingest_bot_value(conn, cfg, other)
    assert admitted is True
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    forged_id, _ = enqueue_outbox(
        conn, channel="feishu_im", action_type="control_receipt",
        destination="oc_control", payload={"text": "forged"},
        idempotency_key="forged-control-receipt", source_event_pk=foreign_pk,
    )
    forged = dict(conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (forged_id,)).fetchone())
    assert outbox_eligible(conn, cfg, forged) is False
    conn.execute("DELETE FROM outbox WHERE outbox_id=?", (forged_id,))

    sent = []

    def runner(argv):
        sent.append(argv)
        return CommandResult({"message_id": "om_control_receipt"}, "bot", [])

    conn.execute(
        "UPDATE global_control_state SET mode='paused' WHERE scope='feishu_support'"
    )
    receipt = deliver_claimed(
        conn, cfg, claim_outbox(conn, worker_id="control-sender"), lark_runner=runner
    )
    assert receipt.remote_id == "om_control_receipt"
    assert sent[0][:4] == ["im", "+messages-send", "--chat-id", "oc_control"]
    invalid = {**event, "message_id": "om_native_control_3", "content": "bug invalid-command"}
    assert ingest_bot_value(conn, cfg, invalid) == (None, False)
    failure = conn.execute(
        "SELECT payload_json FROM outbox WHERE idempotency_key=?",
        ("feishu-native-control:om_native_control_3",),
    ).fetchone()
    assert "不要盲目重发" in failure[0]
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0


def test_native_feishu_mode_receipt_contains_text_commands(conn, config):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["identity"].update({
        "control_operator_id": "owner-operator",
        "feishu_control_user_id": "ou_control",
        "feishu_control_chat_id": "oc_control",
    })
    cfg = Config(validate_config(raw), config.path)
    ensure_global_state(conn)
    conn.execute("UPDATE global_control_state SET mode='stopped' WHERE scope='feishu_support'")
    assert ingest_bot_value(conn, cfg, {
        "message_id": "om_native_mode", "chat_id": "oc_control",
        "chat_type": "p2p", "sender_id": "ou_control",
        "create_time": "1788220800000", "message_type": "text",
        "content": "/feishu",
    }, control_only=True) == (None, False)
    receipt = conn.execute("SELECT payload_json FROM outbox").fetchone()[0]
    assert "mode-action" in receipt
    assert "workbench" in receipt
    assert conn.execute("SELECT count(*) FROM feishu_control_cards").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
    assert ingest_bot_value(conn, cfg, {
        "message_id": "om_stopped_ordinary", "chat_id": "oc_control",
        "chat_type": "p2p", "sender_id": "ou_control",
        "create_time": "1788220800000", "message_type": "text",
        "content": "ordinary support message",
    }, control_only=True) == (None, False)
    assert conn.execute("SELECT count(*) FROM inbound_events").fetchone()[0] == 1
    claimed = claim_outbox(conn, worker_id="stopped-control-sender")
    assert outbox_eligible(conn, cfg, claimed) is True
    sent = []

    def runner(argv):
        sent.append(argv)
        return CommandResult({"message_id": "om_stopped_receipt"}, "bot", [])

    assert deliver_claimed(conn, cfg, claimed, lark_runner=runner).remote_id == "om_stopped_receipt"
    assert sent[0][1] == "+messages-send"


def test_user_poll_routes_owner_control_without_case_admission(conn, config):
    raw = copy.deepcopy(config.raw)
    raw["identity"].update({
        "control_operator_id": "owner-operator",
        "feishu_owner_open_id": "ou_control",
        "feishu_control_user_id": "ou_control",
        "feishu_control_chat_id": "oc_control",
    })
    cfg = Config(validate_config(raw), config.path)
    message = {
        "message_id": "om_polled_control", "chat_id": "oc_control",
        "chat_type": "p2p", "create_time": "1788220799000",
        "sender": {"id": "ou_control"}, "msg_type": "text",
        "content": {"text": "bug list"},
    }

    def runner(argv):
        return CommandResult(
            {"messages": [message] if "p2p" in argv else []}, "user", []
        )

    poll_user_messages(conn, cfg, now=datetime(2026, 9, 1, 0, 0, tzinfo=UTC), runner=runner)
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    assert ingest_bot_value(conn, cfg, {
        "message_id": "om_polled_control", "chat_id": "oc_control",
        "chat_type": "p2p", "sender_id": "ou_control",
        "create_time": "1788220799000", "message_type": "text",
        "content": "bug list",
    }) == (None, False)
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM operator_activities").fetchone()[0] == 0


def test_user_poll_overlaps_and_deduplicates_two_sources(conn, config):
    cfg = ingress_config(config)
    now = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    message = {
        "message_id": "om_poll_1",
        "chat_id": "oc_p2p",
        "chat_type": "p2p",
        "create_time": str(int(now.timestamp() * 1000) - 1000),
        "sender": {"id": "ou_colleague", "name": "同事甲"},
        "chat_name": "私聊会话",
        "msg_type": "text",
        "content": {"text": "K3 help"},
    }
    calls = []

    def runner(argv):
        calls.append(argv)
        return CommandResult({"messages": [message]}, "user", [])

    first = poll_user_messages(conn, cfg, now=now, runner=runner)
    second = poll_user_messages(conn, cfg, now=now, runner=runner)
    assert first["ingested"] == 1
    assert second["ingested"] == 0
    assert conn.execute("SELECT count(*) FROM inbound_events").fetchone()[0] == 1
    payload = conn.execute("SELECT payload_json FROM inbound_events").fetchone()[0]
    assert '"sender_name":"同事甲"' in payload
    assert '"chat_name":"私聊会话"' in payload
    assert all("--as" in argv and argv[-1] == "user" for argv in calls)
    assert any("--is-at-me" in argv for argv in calls)
    search_starts = {
        argv[argv.index("--start") + 1] for argv in calls if "+messages-search" in argv
    }
    assert search_starts == {"2026-09-01T11:55:00+08:00"}
    time_values = [
        argv[argv.index(flag) + 1] for argv in calls for flag in ("--start", "--end")
    ]
    assert all("." not in value for value in time_values)


@pytest.mark.parametrize("first_source", ["feishu_bot_im", "feishu_user_poll"])
def test_bot_stream_and_user_poll_share_one_durable_message_key(conn, first_source):
    first_identity = "bot" if first_source == "feishu_bot_im" else "user"
    second_source = (
        "feishu_user_poll" if first_source == "feishu_bot_im" else "feishu_bot_im"
    )
    second_identity = "user" if first_identity == "bot" else "bot"
    coordinates = {
        "external_id": "om_cross_transport",
        "payload": {"content": "K3 help", "chat_type": "p2p"},
        "occurred_at": "2026-09-01T04:00:00+00:00",
        "sender_id": "ou_colleague",
        "chat_id": "oc_p2p",
    }

    first_pk, first_created = ingest_event(
        conn, source=first_source, identity=first_identity, **coordinates
    )
    second_pk, second_created = ingest_event(
        conn, source=second_source, identity=second_identity, **coordinates
    )

    assert first_created is True
    assert (second_pk, second_created) == (first_pk, False)
    row = conn.execute(
        "SELECT idempotency_key FROM inbound_events WHERE event_pk=?", (first_pk,)
    ).fetchone()
    assert row[0] == "feishu_im:message:om_cross_transport"


@pytest.mark.parametrize("incomplete_call", [1, 2])
def test_incomplete_message_search_never_advances_watermark(
    conn, config, incomplete_call
):
    cfg = ingress_config(config)
    calls = 0

    def runner(_argv):
        nonlocal calls
        calls += 1
        data = {"messages": []}
        if calls == incomplete_call:
            data["has_more"] = True
        return CommandResult(data, "user", [])

    with pytest.raises(IngressError, match="incomplete"):
        poll_user_messages(
            conn,
            cfg,
            now=datetime(2026, 9, 1, 4, 0, tzinfo=UTC),
            runner=runner,
        )
    assert (
        conn.execute(
            "SELECT count(*) FROM watermarks WHERE watermark_key='feishu_user_poll'"
        ).fetchone()[0]
        == 0
    )


def test_normalize_polled_message_accepts_real_cli_local_time():
    normalized = normalize_polled_message(
        {
            "message_id": "om_real_cli",
            "chat_id": "oc_p2p",
            "chat_type": "p2p",
            "create_time": "2026-09-01 19:24",
            "msg_type": "text",
            "content": "K3 status?",
            "sender": {"id": "ou_peer", "sender_type": "user"},
        }
    )

    assert normalized["occurred_at"] == "2026-09-01T11:24:00+00:00"
    assert normalized["payload"]["sender_type"] == "user"


@pytest.mark.parametrize(
    "pagination",
    [{"complete": False}, {"complete": True, "next_token": "synthetic-unread"}],
)
def test_outer_envelope_pagination_never_advances_im_watermark(
    conn, config, pagination
):
    with pytest.raises(IngressError, match="incomplete"):
        poll_user_messages(
            conn,
            ingress_config(config),
            now=datetime(2026, 9, 1, 4, 0, tzinfo=UTC),
            runner=lambda _: CommandResult(
                {"messages": []}, "user", [], {"pagination": pagination}
            ),
        )
    assert not conn.execute(
        "SELECT 1 FROM watermarks WHERE watermark_key='feishu_user_poll'"
    ).fetchone()


def test_normalizers_reject_malformed_coordinates_and_normalize_object_content():
    with pytest.raises(IngressError, match="sender"):
        normalize_polled_message(
            {
                "message_id": "om_bad_sender",
                "chat_id": "oc_p2p",
                "chat_type": "p2p",
                "create_time": "2026-09-01 19:24",
                "sender": "not-an-object",
            }
        )
    with pytest.raises(IngressError, match="chat_type"):
        normalize_polled_message(
            {
                "message_id": "om_bad_type",
                "chat_id": "oc_p2p",
                "create_time": "2026-09-01 19:24",
                "sender": {"id": "ou_peer"},
            }
        )


def test_user_poll_ignores_application_messages(conn, config):
    cfg = ingress_config(config)
    now = datetime(2026, 9, 1, 11, 24, tzinfo=UTC)
    application_message = {
        "message_id": "om_security_notice",
        "chat_id": "oc_security",
        "chat_type": "p2p",
        "create_time": "2026-09-01 19:24",
        "sender": {"id": "cli_security", "sender_type": "app"},
        "msg_type": "interactive",
        "content": "Authorization notice",
    }

    result = poll_user_messages(
        conn,
        cfg,
        now=now,
        runner=lambda argv: CommandResult(
            {"messages": [application_message]}, "user", []
        ),
    )

    assert result["ingested"] == 0
    assert conn.execute("SELECT count(*) FROM inbound_events").fetchone()[0] == 0


def test_failed_second_poll_does_not_advance_watermark(conn, config):
    cfg = ingress_config(config)
    calls = 0

    def runner(argv):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise LarkError("network", error_type="network")
        return CommandResult({"messages": []}, "user", [])

    with pytest.raises(LarkError):
        poll_user_messages(
            conn, cfg, now=datetime(2026, 9, 1, 4, 0, tzinfo=UTC), runner=runner
        )
    assert (
        conn.execute(
            "SELECT count(*) FROM watermarks WHERE watermark_key='feishu_user_poll'"
        ).fetchone()[0]
        == 0
    )


def test_mail_poll_batches_details_overlaps_and_deduplicates(conn, config):
    cfg = ingress_config(config)
    now = datetime(2026, 9, 1, 11, 30, 49, 626010, tzinfo=UTC)
    message_id = "mail-poll-1"
    calls = []

    def runner(argv):
        calls.append(argv)
        if "+triage" in argv:
            return CommandResult(
                {
                    "messages": [{"message_id": message_id}],
                    "has_more": False,
                },
                "user",
                [],
            )
        return CommandResult(
            {
                "messages": [
                    {
                        "message_id": message_id,
                        "subject": "K3 build",
                        "head_from": {"name": "Colleague"},
                        "body_preview": "Build complete",
                        "internal_date": "1788253200000",
                    }
                ],
                "total": 1,
            },
            "user",
            [],
        )

    first = poll_user_mail(conn, cfg, now=now, runner=runner)
    second = poll_user_mail(conn, cfg, now=now, runner=runner)

    assert first["ingested"] == 1
    assert second["ingested"] == 0
    assert (
        conn.execute(
            "SELECT count(*) FROM inbound_events WHERE source='feishu_mail'"
        ).fetchone()[0]
        == 1
    )
    assert sum("+messages" in argv for argv in calls) == 1
    triage = next(argv for argv in calls if "+triage" in argv)
    filter_value = triage[triage.index("--filter") + 1]
    assert "." not in filter_value
    details = next(argv for argv in calls if "+messages" in argv)
    assert "--html=false" in details


def test_mail_poll_does_not_advance_when_detail_batch_is_incomplete(conn, config):
    cfg = ingress_config(config)

    def runner(argv):
        if "+triage" in argv:
            return CommandResult(
                {
                    "messages": [{"message_id": "mail-missing"}],
                    "has_more": False,
                },
                "user",
                [],
            )
        return CommandResult(
            {"messages": [], "unavailable_message_ids": ["mail-missing"]},
            "user",
            [],
        )

    with pytest.raises(IngressError, match="did not return every requested message"):
        poll_user_mail(
            conn,
            cfg,
            now=datetime(2026, 9, 1, 11, 30, tzinfo=UTC),
            runner=runner,
        )
    assert (
        conn.execute(
            "SELECT count(*) FROM watermarks WHERE watermark_key='feishu_mail_poll'"
        ).fetchone()[0]
        == 0
    )


def test_bot_group_requires_configured_group_and_owner_mention(conn, config):
    cfg = ingress_config(config)
    base = {
        "message_id": "om_bot_1",
        "chat_id": "oc_tech",
        "chat_type": "group",
        "sender_id": "ou_colleague",
        "create_time": "1788220800000",
        "message_type": "text",
        "content": "K3 help",
        "mentions": [],
    }
    assert ingest_bot_value(conn, cfg, base) == (None, False)
    base["mentions"] = [{"id": "ou_owner"}]
    event_pk, created = ingest_bot_value(conn, cfg, base)
    assert created is True and event_pk
    loop = {**base, "message_id": "om_bot_2", "content": "[AI 自动回复]loop"}
    assert ingest_bot_value(conn, cfg, loop) == (None, False)
    clarification_loop = {
        **base,
        "message_id": "om_bot_3",
        "content": "[AI 助手确认]loop",
    }
    assert ingest_bot_value(conn, cfg, clarification_loop) == (None, False)
    markdown_loop = {
        **base,
        "message_id": "om_bot_4",
        "content": "### AI 自动回复\n\nloop",
    }
    assert ingest_bot_value(conn, cfg, markdown_loop) == (None, False)


def test_polled_group_requires_actual_owner_mention_even_when_search_returns_it(
    conn, config
):
    cfg = ingress_config(config)
    now = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    group = {
        "message_id": "om_group_false_positive",
        "chat_id": "oc_tech",
        "chat_type": "group",
        "create_time": str(int(now.timestamp() * 1000) - 1000),
        "sender": {"id": "ou_colleague", "sender_type": "user"},
        "msg_type": "text",
        "content": {"text": "K3 help"},
        "mentions": [],
    }

    result = poll_user_messages(
        conn,
        cfg,
        now=now,
        runner=lambda argv: CommandResult(
            {"messages": [group] if "group" in argv else []}, "user", []
        ),
    )

    assert result["ingested"] == 0


def test_inbox_claim_sets_a_real_future_lease(conn):
    event_pk, _ = ingest_event(
        conn,
        source="timer",
        identity="system",
        external_id="lease-test",
        payload={},
        occurred_at=datetime.now(UTC).isoformat(),
    )
    before = datetime.now(UTC)
    assert claim_inbound(conn, worker_id="worker", limit=1) == [event_pk]
    lease = conn.execute(
        "SELECT lease_expires_at FROM inbound_events WHERE event_pk=?", (event_pk,)
    ).fetchone()[0]
    assert (parse_iso(lease) - before).total_seconds() >= 115


def test_inbox_claim_defaults_to_one_event_per_slow_ai_worker_tick(conn):
    event_ids = []
    for sequence in range(3):
        event_pk, _ = ingest_event(
            conn,
            source="timer",
            identity="system",
            external_id=f"lease-batch-{sequence}",
            payload={},
            occurred_at=datetime.now(UTC).isoformat(),
        )
        event_ids.append(event_pk)

    assert claim_inbound(conn, worker_id="worker") == [event_ids[0]]
    states = conn.execute(
        "SELECT status FROM inbound_events ORDER BY received_epoch,event_pk"
    ).fetchall()
    assert [row[0] for row in states].count("claimed") == 1
    assert [row[0] for row in states].count("new") == 2
