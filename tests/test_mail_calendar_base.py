from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from k3_support.approvals import ApprovalError, decide_approval
from k3_support.base_sync import (
    BASE_TABLE_BLUEPRINTS,
    BaseSyncError,
    enqueue_dirty_entities,
    fail_base_sync_job,
    run_base_sync_job,
    sync_case,
)
from k3_support.calendar import (
    CalendarError,
    create_meeting_preview,
    execute_meeting_create,
    normalize_meeting_action,
)
from k3_support.config import Config, validate_config
from k3_support.delivery import DeliveryReceipt, claim_outbox, deliver_claimed
from k3_support.knowledge import create_candidate, review
from k3_support.lark import CommandResult, LarkError
from k3_support.mail import (
    MailError,
    commit_summary_delivery,
    latest_summary_slot,
    prepare_summary,
    refresh_missing_bodies,
    upsert_mail_item,
    validate_ai_summary,
)
from k3_support.operations import heartbeat, reconcile
from k3_support.orchestrator import process_inbound
from k3_support.store import claim_jobs, create_case, ingest_event, transition_case


def configured(config, **features):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"].update(features)
    if raw["features"]["base_sync"]:
        raw["base"] = {
            "app_token": "base_token",
            "cases_table_id": "tbl_cases",
            "knowledge_table_id": "tbl_knowledge",
            "mail_table_id": "tbl_mail",
            "health_table_id": "tbl_health",
        }
    return Config(validate_config(raw), config.path)


def ai_digest_without_important(value):
    assert value["messages"]
    return {
        "overview": "本周期主要是例行信息，没有需要立即处理的事项。",
        "categories": [
            {
                "category": "other",
                "count": len(value["messages"]),
                "summary": "常规进展，可按需查阅。",
            }
        ],
        "important": [],
    }


@pytest.mark.parametrize(
    ("slot", "observed_at", "expected"),
    [
        (
            "mail_noon",
            datetime.fromisoformat("2026-09-03T18:04:21+08:00"),
            "2026-09-03T12:00:00+08:00",
        ),
        (
            "mail_evening",
            datetime.fromisoformat("2026-09-03T18:04:21+08:00"),
            "2026-09-03T18:00:00+08:00",
        ),
        (
            "mail_evening",
            datetime.fromisoformat("2026-09-03T17:59:59+08:00"),
            "2026-09-02T18:00:00+08:00",
        ),
    ],
)
def test_latest_summary_slot_anchors_persistent_timer_to_nominal_time(
    slot, observed_at, expected
):
    assert latest_summary_slot(
        slot,
        timezone="Asia/Shanghai",
        observed_at=observed_at,
    ) == expected


def test_latest_summary_slot_rejects_naive_observation():
    with pytest.raises(MailError, match="observed_at must include timezone"):
        latest_summary_slot(
            "mail_noon",
            timezone="Asia/Shanghai",
            observed_at=datetime.fromisoformat("2026-09-03T12:00:00+08:00").replace(
                tzinfo=None
            ),
        )


def test_mail_summary_advances_watermark_only_after_delivery(conn, config):
    upsert_mail_item(
        conn,
        {
            "message_id": "m1",
            "subject": "K3 weekly info",
            "head_from": {"name": "Alice", "mail_address": "a@example.test"},
            "body_preview": "information",
            "internal_date": str(
                int(
                    datetime.fromisoformat("2026-09-01T10:00:00+08:00").timestamp()
                    * 1000
                )
            ),
        },
    )
    summary = prepare_summary(
        conn,
        scheduled_at="2026-09-01T12:00:00+08:00",
        destination="telegram:owner-chat",
        slot="mail_noon",
        summarizer=ai_digest_without_important,
    )
    assert summary["item_count"] == 1
    assert conn.execute("SELECT count(*) FROM watermarks").fetchone()[0] == 0
    cfg = configured(config, mail=True)
    row = claim_outbox(conn, worker_id="sender")
    deliver_claimed(
        conn,
        cfg,
        row,
        telegram_runner=lambda target, text: DeliveryReceipt(
            "tg-summary-1", {"id": "tg-summary-1"}
        ),
        telegram_button_runner=lambda *_: DeliveryReceipt("tg-summary-1", {"id": "tg-summary-1"}),
    )
    watermark = conn.execute(
        "SELECT value_json FROM watermarks WHERE watermark_key='mail_summary_delivered'"
    ).fetchone()[0]
    assert "2026-09-01T04:00:00+00:00" in watermark

    upsert_mail_item(
        conn,
        {
            "message_id": "m2",
            "subject": "After noon",
            "head_from": {"name": "Bob", "mail_address": "b@example.test"},
            "body_preview": "information",
            "internal_date": str(
                int(
                    datetime.fromisoformat("2026-09-01T15:00:00+08:00").timestamp()
                    * 1000
                )
            ),
        },
    )
    evening = prepare_summary(
        conn,
        scheduled_at="2026-09-01T18:00:00+08:00",
        destination="telegram:owner-chat",
        slot="mail_evening",
        summarizer=ai_digest_without_important,
    )
    assert evening["item_count"] == 1
    assert evening["range_start"] == "2026-09-01T04:00:00+00:00"


def test_out_of_order_summary_delivery_never_moves_watermark_backwards(conn):
    noon = prepare_summary(
        conn,
        scheduled_at="2026-09-01T12:00:00+08:00",
        destination="telegram:owner-chat",
        slot="mail_noon",
    )
    evening = prepare_summary(
        conn,
        scheduled_at="2026-09-01T18:00:00+08:00",
        destination="telegram:owner-chat",
        slot="mail_evening",
    )
    for summary, remote_id in ((evening, "tg-evening"), (noon, "tg-noon")):
        conn.execute(
            "UPDATE outbox SET state='delivered',remote_message_id=? WHERE outbox_id=?",
            (remote_id, summary["outbox_id"]),
        )
        commit_summary_delivery(
            conn, outbox_id=summary["outbox_id"], remote_message_id=remote_id
        )

    watermark = conn.execute(
        "SELECT value_json FROM watermarks WHERE watermark_key='mail_summary_delivered'"
    ).fetchone()[0]
    assert "2026-09-01T10:00:00+00:00" in watermark


def test_mail_summary_backfills_legacy_body_by_exact_message_id(conn):
    internal_date = str(
        int(datetime.fromisoformat("2026-09-01T11:30:00+08:00").timestamp() * 1000)
    )
    event_pk, _ = ingest_event(
        conn,
        source="feishu_mail",
        identity="user",
        external_id="mail_legacy:received",
        payload={
            "message_id": "mail_legacy",
            "subject": "K3 release",
            "internal_date": internal_date,
        },
        occurred_at="2026-09-01T11:30:00+08:00",
    )
    upsert_mail_item(
        conn,
        {
            "message_id": "mail_legacy",
            "subject": "K3 release",
            "internal_date": internal_date,
        },
    )
    calls = []

    result = refresh_missing_bodies(
        conn,
        scheduled_at="2026-09-01T12:00:00+08:00",
        runner=lambda argv: (
            calls.append(argv)
            or CommandResult(
                {
                    "messages": [
                        {
                            "message_id": "mail_legacy",
                            "subject": "K3 release",
                            "internal_date": internal_date,
                            "body_plain_text": "Please review the exact release blocker.",
                        }
                    ]
                },
                "user",
                [],
            )
        ),
    )

    assert result == {"selected": 1, "refreshed": 1}
    assert calls[0][0:4] == ["mail", "+messages", "--message-ids", "mail_legacy"]
    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM inbound_events WHERE event_pk=?", (event_pk,)
        ).fetchone()[0]
    )
    assert payload["body_plain_text"] == "Please review the exact release blocker."


def test_mail_body_backfill_fails_closed_on_partial_batch(conn):
    internal_date = str(
        int(datetime.fromisoformat("2026-09-01T11:30:00+08:00").timestamp() * 1000)
    )
    for message_id in ("mail_one", "mail_two"):
        ingest_event(
            conn,
            source="feishu_mail",
            identity="user",
            external_id=f"{message_id}:received",
            payload={"message_id": message_id, "internal_date": internal_date},
            occurred_at="2026-09-01T11:30:00+08:00",
        )
        upsert_mail_item(
            conn,
            {"message_id": message_id, "internal_date": internal_date},
        )

    with pytest.raises(MailError, match="every exact message"):
        refresh_missing_bodies(
            conn,
            scheduled_at="2026-09-01T12:00:00+08:00",
            runner=lambda _argv: CommandResult(
                {
                    "messages": [
                        {
                            "message_id": "mail_one",
                            "internal_date": internal_date,
                            "body_plain_text": "one",
                        }
                    ]
                },
                "user",
                [],
            ),
        )
    assert conn.execute(
        """SELECT count(*) FROM inbound_events
             WHERE source='feishu_mail'
               AND coalesce(json_extract(payload_json,'$.body_plain_text'),'')!=''"""
    ).fetchone()[0] == 0


def test_mail_inbound_indexes_item_and_links_case_for_real_summary(conn, config):
    payload = {
        "message_id": "mail_e2e_1",
        "thread_id": "thread_e2e",
        "subject": "紧急：K3 发布阻塞",
        "body_preview": "需要今天处理",
        "head_from": {"name": "同事", "mail_address": "peer@example.com"},
        "internal_date": str(
            int(datetime.fromisoformat("2026-09-01T11:30:00+08:00").timestamp() * 1000)
        ),
        "label_ids": [],
    }
    event_pk, _ = ingest_event(
        conn,
        source="feishu_mail",
        identity="user",
        external_id="mail_e2e_1:received",
        payload=payload,
        occurred_at="2026-09-01T11:30:00+08:00",
        sender_id="peer@example.com",
        chat_id="me",
    )
    result = process_inbound(
        conn, event_pk=event_pk, worker_id="mail-worker", config=config
    )
    item = conn.execute(
        "SELECT message_id,classification,case_id FROM mail_items WHERE message_id='mail_e2e_1'"
    ).fetchone()
    assert tuple(item) == ("mail_e2e_1", "urgent_action", result["case_id"])
    summary = prepare_summary(
        conn,
        scheduled_at="2026-09-01T12:00:00+08:00",
        destination="telegram:owner-chat",
        slot="mail_noon",
        summarizer=lambda value: {
            "overview": "有一项发布阻塞需要处理。",
            "categories": [
                {"category": "project_release", "count": 1, "summary": "发布受阻。"}
            ],
            "important": [
                {
                    "message_id": "mail_e2e_1",
                    "summary": "发布流程被一个 K3 问题阻塞，需要确认处理方案。",
                    "why_important": "影响当天发布",
                    "action": "查看原邮件并决定处理人",
                    "deadline": "今天",
                }
            ],
        },
        share_destination="oc_owner_private",
    )
    assert summary["item_count"] == 1
    assert summary["state"] == "linking"
    assert "text" not in summary

    cfg = configured(config, mail=True)
    share = claim_outbox(
        conn, worker_id="sender", eligible=lambda row: row["channel"] == "mail"
    )
    assert (share["channel"], share["action_type"]) == ("mail", "share_to_owner")
    deliver_claimed(
        conn,
        cfg,
        share,
        mail_runner=lambda argv: CommandResult(
            {"card_id": "card-1", "im_message_id": "om_shared_mail"}, "user", []
        ),
    )
    resolve = claim_outbox(
        conn, worker_id="sender", eligible=lambda row: row["channel"] == "mail"
    )
    assert (resolve["channel"], resolve["action_type"]) == ("mail", "resolve_app_link")
    deliver_claimed(
        conn,
        cfg,
        resolve,
        lark_runner=lambda argv: CommandResult(
            {
                "messages": [
                    {
                        "message_id": "om_shared_mail",
                        "message_app_link": "https://applink.feishu.cn/client/chat/open?position=1",
                    }
                ]
            },
            "user",
            [],
        ),
    )
    telegram = claim_outbox(
        conn,
        worker_id="sender",
        eligible=lambda row: row["action_type"] == "mail_summary",
    )
    assert (telegram["channel"], telegram["action_type"]) == ("telegram", "mail_summary")
    text = json.loads(telegram["payload_json"])["text"]
    assert "https://applink.feishu.cn/client/chat/open?position=1" in text
    assert "需要今天处理" not in text


def test_ai_summary_failure_sends_nothing_and_keeps_watermark(conn):
    upsert_mail_item(
        conn,
        {
            "message_id": "mail-model-failure",
            "subject": "Do not dump this subject",
            "body_preview": "Do not dump this body",
            "internal_date": str(
                int(datetime.fromisoformat("2026-09-01T10:00:00+08:00").timestamp() * 1000)
            ),
        },
    )

    with pytest.raises(MailError, match="summarizer failed"):
        prepare_summary(
            conn,
            scheduled_at="2026-09-01T12:00:00+08:00",
            destination="telegram:owner-chat",
            slot="mail_noon",
            summarizer=lambda _: None,
        )

    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM mail_digest_runs").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM watermarks").fetchone()[0] == 0


def test_ai_summary_rejects_invented_message_id():
    with pytest.raises(MailError, match="unknown or duplicate"):
        validate_ai_summary(
            {
                "overview": "one item",
                "categories": [
                    {"category": "other", "count": 1, "summary": "one item"}
                ],
                "important": [
                    {
                        "message_id": "invented",
                        "summary": "summary",
                        "why_important": "reason",
                        "action": None,
                        "deadline": None,
                    }
                ],
            },
            allowed_message_ids={"real"},
            max_important=8,
        )


def test_link_lookup_retry_does_not_share_mail_twice(conn, config):
    upsert_mail_item(
        conn,
        {
            "message_id": "mail-link-retry",
            "subject": "Private raw subject",
            "body_preview": "Private raw body",
            "internal_date": str(
                int(datetime.fromisoformat("2026-09-01T10:00:00+08:00").timestamp() * 1000)
            ),
        },
    )
    prepare_summary(
        conn,
        scheduled_at="2026-09-01T12:00:00+08:00",
        destination="telegram:owner-chat",
        slot="mail_noon",
        summarizer=lambda _: {
            "overview": "有一封需要决定的邮件。",
            "categories": [
                {"category": "other", "count": 1, "summary": "技术事项待处理。"}
            ],
            "important": [
                {
                    "message_id": "mail-link-retry",
                    "summary": "有一项技术决定待处理。",
                    "why_important": "需要你决定",
                    "action": "查看邮件",
                    "deadline": None,
                }
            ],
        },
        share_destination="oc_owner_private",
    )
    cfg = configured(config, mail=True)
    calls = {"share": 0, "resolve": 0}
    share = claim_outbox(conn, worker_id="sender")

    def share_runner(argv):
        calls["share"] += 1
        return CommandResult({"im_message_id": "om_retry"}, "user", [])

    deliver_claimed(conn, cfg, share, mail_runner=share_runner)
    resolver = claim_outbox(conn, worker_id="sender")

    def unavailable(_argv):
        calls["resolve"] += 1
        raise LarkError("temporary", error_type="transport")

    with pytest.raises(LarkError, match="temporary"):
        deliver_claimed(conn, cfg, resolver, lark_runner=unavailable)
    assert conn.execute(
        "SELECT state FROM outbox WHERE outbox_id=?", (resolver["outbox_id"],)
    ).fetchone()[0] == "retry"
    conn.execute(
        "UPDATE outbox SET next_attempt_at=NULL WHERE outbox_id=?", (resolver["outbox_id"],)
    )
    resolver_retry = claim_outbox(conn, worker_id="sender")
    deliver_claimed(
        conn,
        cfg,
        resolver_retry,
        lark_runner=lambda argv: CommandResult(
            {
                "messages": [
                    {
                        "message_id": "om_retry",
                        "message_app_link": "https://applink.feishu.cn/client/chat/open?position=retry",
                    }
                ]
            },
            "user",
            [],
        ),
    )
    assert calls == {"share": 1, "resolve": 1}
    telegram = claim_outbox(conn, worker_id="sender")
    text = json.loads(telegram["payload_json"])["text"]
    assert "position=retry" in text
    assert "Private raw subject" not in text
    assert "Private raw body" not in text


def test_uncertain_mail_share_is_not_replayed_after_sender_crash(conn):
    upsert_mail_item(
        conn,
        {
            "message_id": "mail-share-crash",
            "subject": "Important source",
            "body_preview": "Needs review",
            "internal_date": str(
                int(
                    datetime.fromisoformat("2026-09-01T10:00:00+08:00").timestamp()
                    * 1000
                )
            ),
        },
    )
    prepare_summary(
        conn,
        scheduled_at="2026-09-01T12:00:00+08:00",
        destination="telegram:owner-chat",
        slot="mail_noon",
        summarizer=lambda _: {
            "overview": "有一项待处理。",
            "categories": [
                {"category": "other", "count": 1, "summary": "工作事项待处理。"}
            ],
            "important": [
                {
                    "message_id": "mail-share-crash",
                    "summary": "一项工作需要审核。",
                    "why_important": "需要人工判断",
                    "action": "查看邮件",
                    "deadline": None,
                }
            ],
        },
        share_destination="oc_owner_private",
    )
    share = claim_outbox(conn, worker_id="sender")
    assert share["action_type"] == "share_to_owner"
    from k3_support.db import transaction
    from k3_support.delivery_attempts import start_attempt

    # A crash after merely claiming now has durable proof no call started. This
    # fixture models the ambiguous window after dispatch actually began.
    with transaction(conn):
        assert start_attempt(conn, share)
    conn.execute(
        "UPDATE outbox SET lease_expires_at='2026-01-01T00:00:00+00:00' WHERE outbox_id=?",
        (share["outbox_id"],),
    )

    result = reconcile(conn)

    assert result["uncertain_outbox"] == 1
    assert conn.execute(
        "SELECT state FROM outbox WHERE outbox_id=?", (share["outbox_id"],)
    ).fetchone()[0] == "permanent_failure"
    assert conn.execute("SELECT state FROM mail_digest_links").fetchone()[0] == "failed"
    telegram = claim_outbox(conn, worker_id="sender")
    assert telegram["action_type"] == "mail_summary"
    assert "链接生成失败" in json.loads(telegram["payload_json"])["text"]
    assert conn.execute(
        "SELECT count(*) FROM outbox WHERE action_type='share_to_owner'"
    ).fetchone()[0] == 1


def _mail_route(route: str, *, confidence: float = 0.96, reason: str) -> dict:
    return {
        "route": route,
        "confidence": confidence,
        "issue_type": "mail",
        "severity": "P3",
        "domain": "bootloader",
        "repository_hints": [],
        "reason_codes": [reason],
        "clarification_question": None,
        "fallback_route": None,
        "requires_owner_judgment": False,
        "conversation_relation": "standalone",
    }


def test_semantic_mail_noise_is_summarized_without_creating_case(conn, config):
    event_pk, _ = ingest_event(
        conn,
        source="feishu_mail",
        identity="user",
        external_id="mail_noise:received",
        payload={
            "message_id": "mail_noise",
            "subject": "周末团建照片",
            "body_preview": "照片已上传",
            "internal_date": "1788230000000",
        },
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="peer@example.test",
        chat_id="me",
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="mail-worker",
        config=config,
        message_router=lambda _: _mail_route(
            "ignore", reason="non_work_noise"
        ),
    )

    assert result["ignored"] is True
    assert result["case_id"] is None
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
    item = conn.execute(
        "SELECT classification,requested_action,case_id FROM mail_items"
    ).fetchone()
    assert tuple(item) == ("noise", None, None)


def test_high_confidence_mail_research_is_visible_as_action(conn, config):
    event_pk, _ = ingest_event(
        conn,
        source="feishu_mail",
        identity="user",
        external_id="mail_action:received",
        payload={
            "message_id": "mail_action",
            "subject": "请确认 K3 UFS 启动支持情况",
            "body_preview": "请查资料后回复支持范围",
            "internal_date": "1788230000000",
        },
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="peer@example.test",
        chat_id="me",
    )
    result = process_inbound(
        conn,
        event_pk=event_pk,
        worker_id="mail-worker",
        config=config,
        message_router=lambda _: _mail_route(
            "research", reason="source_lookup_needed"
        ),
    )

    assert result["route"]["route"] == "research"
    item = conn.execute(
        "SELECT classification,requested_action,case_id FROM mail_items"
    ).fetchone()
    assert tuple(item) == ("action", "research", result["case_id"])
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_meeting_requires_exact_owner_approval_and_user_identity(conn, config):
    from test_meeting_recovery import CalendarFixture

    from k3_support.meeting_recovery import bind_meeting_action

    cfg = configured(config, calendar=True)
    cfg.raw['identity']['feishu_owner_open_id'] = 'ou_owner'
    transport = CalendarFixture()
    transport.event_id = 'event_1'
    transport.attendee_pages = [{'items': [
        {'type': 'user', 'user_id': 'ou_colleague', 'rsvp_status': 'accept'},
        {'type': 'resource', 'room_id': 'omm_room', 'rsvp_status': 'accept'}], 'has_more': False}]
    case_id, _ = create_case(
        conn, title="meeting", case_type="meeting", severity="P3", confidence=0.8
    )
    action = normalize_meeting_action(
        case_id=case_id,
        summary="K3 问题讨论",
        start="2027-01-05T14:00:00+08:00",
        end="2027-01-05T14:30:00+08:00",
        attendee_ids=["ou_colleague"],
        room_ids=["omm_room"],
        description="议程：K3 启动问题",
    )
    action = bind_meeting_action(cfg, action, runner=transport)
    transport.action = action
    preview = create_meeting_preview(conn, action=action)
    with pytest.raises(Exception, match="unconsumed exact approval"):
        execute_meeting_create(
            conn, cfg, preview_id=preview["preview_id"], runner=lambda argv: None
        )
    decide_approval(
        conn,
        cfg,
        approval_id=preview["approval_id"],
        approve=True,
        approver_user_id="owner-user",
        approver_chat_id="owner-chat",
        message_id="tg-meeting",
        decision_text=f"approve meeting {preview['action_digest']}",
        expected_digest=preview["action_digest"],
    )
    result = execute_meeting_create(
        conn, cfg, preview_id=preview["preview_id"], runner=transport
    )
    assert result["event_id"] == "event_1"
    invites = next(call for call in transport.calls if call[:3] == ['calendar', 'event.attendees', 'create'])
    assert json.loads(invites[invites.index('--data') + 1]) == action['attendees_body']
    assert all(call[call.index('--as') + 1] == 'user' for call in transport.calls)
    with pytest.raises((ApprovalError, CalendarError)):
        execute_meeting_create(
            conn, cfg, preview_id=preview["preview_id"], runner=transport
        )
    replay = create_meeting_preview(conn, action=action)
    assert replay["preview_id"] == preview["preview_id"]
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1


def test_uncertain_meeting_preview_cannot_be_reissued_without_reconciliation(conn, config):
    from test_meeting_recovery import CalendarFixture

    from k3_support.meeting_recovery import bind_meeting_action

    cfg = configured(config, calendar=True)
    cfg.raw['identity']['feishu_owner_open_id'] = 'ou_owner'
    transport = CalendarFixture()
    case_id, _ = create_case(
        conn,
        title="meeting retry",
        case_type="meeting",
        severity="P3",
        confidence=0.8,
    )
    action = normalize_meeting_action(
        case_id=case_id,
        summary="K3 重试讨论",
        start="2027-01-05T15:00:00+08:00",
        end="2027-01-05T15:30:00+08:00",
        attendee_ids=["ou_colleague"],
    )
    action = bind_meeting_action(cfg, action, runner=transport)
    transport.action = action
    first = create_meeting_preview(conn, action=action)
    decide_approval(
        conn,
        cfg,
        approval_id=first["approval_id"],
        approve=True,
        approver_user_id="owner-user",
        approver_chat_id="owner-chat",
        message_id="tg-meeting-fail",
        decision_text=f"approve meeting {first['action_digest']}",
        expected_digest=first["action_digest"],
    )
    transport.fail = 'calendar events create'
    with pytest.raises(CalendarError, match="recovery"):
        execute_meeting_create(
            conn,
            cfg,
            preview_id=first["preview_id"],
            runner=transport,
        )

    second = create_meeting_preview(conn, action=action)

    assert second["preview_id"] == first["preview_id"]
    assert second["approval_id"] == first["approval_id"]
    assert (
        conn.execute(
            "SELECT status FROM meeting_previews WHERE preview_id=?",
            (first["preview_id"],),
        ).fetchone()[0]
        == "creating"
    )


def test_expired_meeting_preview_can_be_reissued(conn):
    case_id, _ = create_case(
        conn, title="meeting", case_type="meeting", severity="P3", confidence=0.8
    )
    action = normalize_meeting_action(
        case_id=case_id,
        summary="K3 review",
        start="2027-01-05T16:00:00+08:00",
        end="2027-01-05T16:30:00+08:00",
        attendee_ids=["ou_colleague"],
    )
    first = create_meeting_preview(conn, action=action)
    conn.execute(
        "UPDATE approvals SET expires_at=? WHERE approval_id=?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), first["approval_id"]),
    )

    second = create_meeting_preview(conn, action=action)

    assert second["preview_id"] != first["preview_id"]
    assert second["approval_id"] != first["approval_id"]
    first_status = conn.execute(
        """SELECT mp.status,a.status FROM meeting_previews mp
           JOIN approvals a USING(approval_id) WHERE mp.preview_id=?""",
        (first["preview_id"],),
    ).fetchone()
    second_status = conn.execute(
        """SELECT mp.status,a.status FROM meeting_previews mp
           JOIN approvals a USING(approval_id) WHERE mp.preview_id=?""",
        (second["preview_id"],),
    ).fetchone()
    assert tuple(first_status) == ("expired", "expired")
    assert tuple(second_status) == ("preview", "requested")


def test_base_is_one_way_versioned_mirror(conn, config):
    cfg = configured(config, base_sync=True)
    case_id, _ = create_case(
        conn, title="bug", case_type="bug", severity="P2", confidence=0.4
    )
    calls = []

    def runner(argv):
        calls.append(argv)
        if "+record-batch-create" in argv:
            return CommandResult({"record_id_list": ["rec_1"]}, "user", [])
        return CommandResult({}, "user", [])

    created = sync_case(conn, cfg, case_id=case_id, runner=runner)
    assert created == {"case_id": case_id, "record_id": "rec_1", "version": 1}
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="ready",
        expected_version=1,
    )
    updated = sync_case(conn, cfg, case_id=case_id, runner=runner)
    assert updated["version"] == 2
    assert "+record-search" in calls[0]
    assert "+record-batch-create" in calls[1]
    assert "+record-batch-update" in calls[2]
    mapping = conn.execute(
        "SELECT mirrored_version FROM base_mappings WHERE entity_id=?", (case_id,)
    ).fetchone()[0]
    assert mapping == 2
    assert set(BASE_TABLE_BLUEPRINTS) == {
        "Cases",
        "Knowledge Review",
        "Mail Digest",
        "System Health",
    }


def test_base_queues_and_idempotently_mirrors_all_management_entities(conn, config):
    cfg = configured(config, base_sync=True)
    case_id, _ = create_case(
        conn, title="boot issue", case_type="bug", severity="P2", confidence=0.4
    )
    knowledge_id = create_candidate(
        conn,
        title="K3 boot FAQ",
        questions=["K3 怎么启动"],
        answer_markdown="Use the reviewed procedure.",
        project="K3",
        module="boot",
        software_version="v1",
        disclosure_class="internal",
        confidence=0.9,
        source_authority=0.8,
        canonical_case_id=case_id,
        source_digest="source-1",
    )
    upsert_mail_item(
        conn,
        {
            "message_id": "mail-base-1",
            "subject": "K3 action",
            "head_from": {"name": "Alice", "mail_address": "alice@example.test"},
            "body_preview": "please review",
            "internal_date": "1788230000000",
        },
        case_id=case_id,
    )
    heartbeat(
        conn,
        "worker",
        "ready",
        {
            "inbox_depth": 2,
            "secret": "must-not-leak",
            "error": "RuntimeError: /private/runtime/path",
        },
    )

    queued = enqueue_dirty_entities(conn, cfg)
    assert queued["queued"] == 4
    jobs = claim_jobs(conn, "base-worker", limit=10, job_types=("base_sync",))
    assert len(jobs) == 4
    calls = []

    def runner(argv):
        calls.append(argv)
        return CommandResult(
            {"record_id_list": [f"rec_{len(calls)}"]}
            if "+record-batch-create" in argv
            else {},
            "user",
            [],
        )

    for job in jobs:
        from k3_support.base_sync_attempt import AttemptRef
        run_base_sync_job(conn, cfg, job_id=job["job_id"], attempt_ref=AttemptRef.from_claim(job), runner=runner)
    assert conn.execute("SELECT count(*) FROM base_mappings").fetchone()[0] == 4
    assert enqueue_dirty_entities(conn, cfg) == {
        "enabled": True,
        "queued": 0,
        "unchanged": 4,
        "exhausted": [],
    }
    health_payload = next(
        argv[argv.index("--json") + 1]
        for argv in calls
        if "tbl_health" in argv and "--json" in argv
    )
    assert "must-not-leak" not in health_payload
    assert "/private/runtime/path" not in health_payload
    assert "RuntimeError" in health_payload

    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    assert enqueue_dirty_entities(conn, cfg)["queued"] == 1
    changed = claim_jobs(conn, "base-worker", limit=1, job_types=("base_sync",))[0]
    run_base_sync_job(conn, cfg, job_id=changed["job_id"], attempt_ref=AttemptRef.from_claim(changed), runner=runner)
    assert "+record-batch-update" in calls[-1]


def test_base_recovers_uncertain_create_by_exact_business_key_lookup(conn, config):
    cfg = configured(config, base_sync=True)
    case_id, _ = create_case(
        conn, title="recover", case_type="bug", severity="P2", confidence=0.3
    )
    calls = []

    def runner(argv):
        calls.append(argv)
        if "+record-search" in argv:
            return CommandResult(
                {
                    "fields": ["Case ID", "标题"],
                    "data": [[case_id, "recover"]],
                    "record_id_list": ["rec_remote"],
                },
                "user",
                [],
            )
        return CommandResult({}, "user", [])

    result = sync_case(conn, cfg, case_id=case_id, runner=runner)
    assert result["record_id"] == "rec_remote"
    assert [argv[1] for argv in calls] == [
        "+record-search",
        "+record-batch-update",
    ]
    assert all(argv[argv.index("--format") + 1] == "json" for argv in calls)


def test_base_duplicate_matrix_matches_fail_closed(conn, config):
    cfg = configured(config, base_sync=True)
    case_id, _ = create_case(
        conn, title="duplicate", case_type="bug", severity="P2", confidence=0.3
    )
    calls = []

    def runner(argv):
        calls.append(argv)
        return CommandResult(
            {
                "fields": ["Case ID"],
                "data": [[case_id], [case_id]],
                "record_id_list": ["rec_one", "rec_two"],
            },
            "user",
            [],
        )

    with pytest.raises(BaseSyncError, match="duplicate records"):
        sync_case(conn, cfg, case_id=case_id, runner=runner)
    assert [argv[1] for argv in calls] == ["+record-search"]


def test_base_sync_failure_is_retried_without_blocking_case(conn, config):
    cfg = configured(config, base_sync=True)
    case_id, _ = create_case(
        conn, title="retry", case_type="bug", severity="P2", confidence=0.2
    )
    enqueue_dirty_entities(conn, cfg)
    job = claim_jobs(conn, "base-worker", limit=1, job_types=("base_sync",))[0]

    def unavailable(_argv):
        raise RuntimeError("Base unavailable")

    with pytest.raises(RuntimeError, match="Base unavailable"):
        from k3_support.base_sync_attempt import AttemptRef
        run_base_sync_job(conn, cfg, job_id=job["job_id"], attempt_ref=AttemptRef.from_claim(job), runner=unavailable)
    failure = fail_base_sync_job(
        conn, job_id=job["job_id"], attempt_ref=AttemptRef.from_claim(job), error=RuntimeError("Base unavailable")
    )
    assert failure["state"] == "queued"
    assert (
        conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()[
            0
        ]
        == "intake"
    )
