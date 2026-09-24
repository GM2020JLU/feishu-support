from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from k3_support.approvals import ApprovalError, decide_approval
from k3_support.calendar import (
    CalendarError,
    create_meeting_preview,
    exact_meeting_preview,
    execute_meeting_create,
    historical_meeting_preview,
    normalize_meeting_action,
)
from k3_support.config import Config
from k3_support.control import ControlMessage
from k3_support.db import connect, migrate, migration_files
from k3_support.ids import canonical_json, digest
from k3_support.lark import CommandResult
from k3_support.meeting_recovery import (
    CalendarReader,
    MeetingRecoveryError,
    RecoveryBudget,
    begin_attempt,
    bind_existing_meeting,
    bind_meeting_action,
    cancel_before_dispatch,
    check_meeting_creation,
    enter_dispatch,
    finish_invitation,
    meeting_recovery_report,
    prepare_legacy_meeting_preview,
    prepare_meeting_successor,
    record_event_receipt,
    record_failure,
    resolve_calendar_target,
    revise_bound_action,
)
from k3_support.store import create_case
from k3_support.timeutil import iso_now

OWNER = ControlMessage("owner-user", "owner-chat", "recovery-fixture", "recovery")


def configured(config):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"]["calendar"] = True
    raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    return Config(raw, config.path)


class CalendarFixture:
    """Synthetic only: every read/write is an in-process contract fixture."""

    def __init__(self):
        self.calls = []
        self.timeouts = []
        self.action = None
        self.calendar_id = "fixture_calendar@example.invalid"
        self.owner = "ou_owner"
        self.identity = "user"
        self.event_id = "fixture_event_0"
        self.pages = [
            {
                "items": [{"summary": "恢复测试", "event_id": self.event_id}],
                "has_more": False,
            }
        ]
        self.attendee_pages = [
            {
                "items": [
                    {
                        "type": "user",
                        "user_id": "ou_peer",
                        "rsvp_status": "needs_action",
                    }
                ],
                "has_more": False,
            }
        ]
        self.overrides = {}
        self.fail = None
        self.hook = None

    def event(self, event_id=None):
        from k3_support.meeting_recovery import _bodies

        return {
            **self.action.get("create_body", _bodies(self.action)[0]),
            "event_id": event_id or self.event_id,
            "organizer_calendar_id": self.calendar_id,
            "status": "confirmed",
            "is_exception": False,
            **self.overrides,
        }

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.timeouts.append(kwargs.get("timeout"))
        if self.hook:
            self.hook(argv)
        kind = " ".join(argv[:3])
        if self.fail == kind:
            raise TimeoutError("synthetic timeout")
        if argv[:3] == ["calendar", "calendars", "primary"]:
            data = {
                "calendars": [
                    {
                        "user_id": self.owner,
                        "calendar": {
                            "calendar_id": self.calendar_id,
                            "type": "primary",
                            "role": "owner",
                            "is_deleted": False,
                            "is_third_party": False,
                        },
                    }
                ]
            }
        elif argv[:3] == ["calendar", "events", "create"]:
            assert (
                json.loads(argv[argv.index("--data") + 1]) == self.action["create_body"]
            )
            assert (
                argv[argv.index("--idempotency-key") + 1] == self.action["operation_id"]
            )
            data = {"event": self.event()}
        elif argv[:3] == ["calendar", "event.attendees", "create"]:
            assert (
                json.loads(argv[argv.index("--data") + 1])
                == self.action["attendees_body"]
            )
            data = {"attendees": self.attendee_pages[0]["items"]}
        elif argv[:2] == ["api", "GET"]:
            params = json.loads(argv[argv.index("--params") + 1])
            assert (
                "anchor_time" in params
                and not {"start_time", "end_time", "sync_token"} & params.keys()
            )
            index = int(params.get("page_token", "0"))
            data = self.pages[index]
        elif argv[:3] == ["calendar", "events", "get"]:
            data = {"event": self.event(argv[argv.index("--event-id") + 1])}
        elif argv[:3] == ["calendar", "event.attendees", "list"]:
            index = (
                int(argv[argv.index("--page-token") + 1])
                if "--page-token" in argv
                else 0
            )
            data = self.attendee_pages[index]
        else:
            raise AssertionError(f"unreviewed fixture command {argv}")
        return CommandResult(data, self.identity, [])


def prepared(conn, config, *, attendees=None, legacy=False):
    cfg = configured(config)
    transport = CalendarFixture()
    case_id, _ = create_case(
        conn, title="恢复测试", case_type="meeting", severity="P3", confidence=0.98
    )
    action = normalize_meeting_action(
        case_id=case_id,
        summary="恢复测试",
        start="2028-01-05T14:00:00+05:45",
        end="2028-01-05T14:30:00+05:45",
        attendee_ids=attendees if attendees is not None else ["ou_peer"],
        description="核对议程",
        timezone="Asia/Kathmandu",
    )
    if not legacy:
        action = bind_meeting_action(cfg, action, runner=transport)
    transport.action = action
    preview = create_meeting_preview(conn, action=action)
    decide_approval(
        conn,
        cfg,
        approval_id=preview["approval_id"],
        approve=True,
        approver_user_id=OWNER.user_id,
        approver_chat_id=OWNER.chat_id,
        message_id="approve",
        decision_text="exact",
        expected_digest=preview["action_digest"],
    )
    return cfg, transport, action, preview


def uncertain(conn, config, **kwargs):
    cfg, transport, action, preview = prepared(conn, config, **kwargs)
    attempt = begin_attempt(conn, cfg, preview["preview_id"])
    enter_dispatch(
        conn,
        cfg,
        attempt_id=attempt["attempt_id"],
        dispatch_token=attempt["dispatch_token"],
    )
    record_failure(conn, attempt_id=attempt["attempt_id"], error=TimeoutError())
    return cfg, transport, action, preview, attempt


def check(conn, cfg, transport, preview, **kwargs):
    transport.calls.clear()
    result = check_meeting_creation(
        conn,
        cfg,
        preview_id=preview["preview_id"],
        reader=CalendarReader(transport),
        **kwargs,
    )
    assert all(
        argv[:3]
        not in (
            ["calendar", "events", "create"],
            ["calendar", "event.attendees", "create"],
        )
        for argv in transport.calls
    )
    return result


def test_exact_v2_create_receipt_precedes_independent_invitation(conn, config):
    cfg, transport, action, preview = prepared(conn, config)

    def verify_receipt(argv):
        if argv[:3] == ["calendar", "event.attendees", "create"]:
            assert (
                conn.execute(
                    "SELECT count(*) FROM meeting_recovery_observations WHERE kind='event_receipt'"
                ).fetchone()[0]
                == 1
            )
            assert not conn.in_transaction

    transport.hook = verify_receipt
    result = execute_meeting_create(
        conn, cfg, preview_id=preview["preview_id"], runner=transport
    )
    assert result["outcome"] == "complete"
    assert result["invitation_membership_confirmed"]
    assert (
        conn.execute("SELECT status FROM meeting_previews").fetchone()[0] == "created"
    )
    writes = [argv for argv in transport.calls if argv[2] == "create"]
    assert len(writes) == 2
    assert all(
        argv[argv.index("--calendar-id") + 1] == action["target"]["calendar_id"]
        for argv in writes
    )
    assert not any("+create" in argv or "delete" in argv for argv in transport.calls)
    with pytest.raises(CalendarError):
        execute_meeting_create(
            conn, cfg, preview_id=preview["preview_id"], runner=transport
        )
    assert len([argv for argv in transport.calls if argv[2] == "create"]) == 2


def test_invitation_timeout_preserves_event_and_never_replays(conn, config):
    cfg, transport, _, preview = prepared(conn, config)
    transport.fail = "calendar event.attendees create"
    with pytest.raises(CalendarError):
        execute_meeting_create(
            conn, cfg, preview_id=preview["preview_id"], runner=transport
        )
    report = meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])
    assert report["phase"] == "partial" and report["event_id"] == transport.event_id
    assert [item["kind"] for item in report["observations"]].count("event_receipt") == 1
    assert not report["can_repreview"] and not report["can_auto_retry"]


def test_room_pending_is_not_booked(conn, config):
    cfg, transport, _, preview = prepared(conn, config, attendees=["omm_fixture"])
    transport.attendee_pages = [
        {
            "items": [
                {
                    "type": "resource",
                    "room_id": "omm_fixture",
                    "rsvp_status": "needs_action",
                }
            ],
            "has_more": False,
        }
    ]
    result = execute_meeting_create(
        conn, cfg, preview_id=preview["preview_id"], runner=transport
    )
    assert result["invitation_membership_confirmed"] is True
    assert result["rooms_booked"] is False and result["room_statuses"] == {
        "omm_fixture": "needs_action"
    }


def test_empty_invite_list_uses_no_invitation_transport(conn, config):
    cfg, transport, _, preview = prepared(conn, config, attendees=[])
    assert (
        execute_meeting_create(
            conn, cfg, preview_id=preview["preview_id"], runner=transport
        )["outcome"]
        == "complete"
    )
    assert not any(
        argv[:3] == ["calendar", "event.attendees", "create"]
        for argv in transport.calls
    )


@pytest.mark.parametrize("change", ["takeover", "cancelled", "round", "global"])
def test_create_receipt_survives_authority_loss_without_invitation(
    conn, config, change
):
    cfg, transport, action, preview = prepared(conn, config)

    def lose_authority(argv):
        if argv[:3] != ["calendar", "events", "create"]:
            return
        if change == "round":
            conn.execute(
                "UPDATE cases SET lifecycle_round=lifecycle_round+1 WHERE case_id=?",
                (action["case_id"],),
            )
        elif change == "global":
            from k3_support.runtime_control import ensure_global_state

            ensure_global_state(conn)
        else:
            conn.execute(
                "UPDATE cases SET state=? WHERE case_id=?", (change, action["case_id"])
            )

    transport.hook = lose_authority
    with pytest.raises(CalendarError):
        execute_meeting_create(
            conn, cfg, preview_id=preview["preview_id"], runner=transport
        )
    report = meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])
    assert report["event_id"] == transport.event_id and report["phase"] == "partial"
    assert any(item["kind"] == "event_receipt" for item in report["observations"])
    assert not any(
        argv[:3] == ["calendar", "event.attendees", "create"]
        for argv in transport.calls
    )


def test_never_dispatched_proof_old_executor_fenced_fresh_approval_only(
    conn, config, tmp_path
):
    cfg, transport, _, preview = prepared(conn, config)
    attempt = begin_attempt(conn, cfg, preview["preview_id"])
    second = connect(cfg.database_path)
    # The fixture conn and config both point at the same isolated database.
    cancel_before_dispatch(
        second,
        cfg,
        attempt_id=attempt["attempt_id"],
        expected_revision=1,
        control_message=OWNER,
    )
    with pytest.raises(MeetingRecoveryError):
        enter_dispatch(
            conn,
            cfg,
            attempt_id=attempt["attempt_id"],
            dispatch_token=attempt["dispatch_token"],
        )
    successor = prepare_meeting_successor(
        second,
        cfg,
        attempt_id=attempt["attempt_id"],
        expected_revision=2,
        control_message=OWNER,
    )
    assert successor["requires_approval"]
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?",
            (successor["approval_id"],),
        ).fetchone()[0]
        == "requested"
    )
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?",
            (preview["approval_id"],),
        ).fetchone()[0]
        == "consumed"
    )
    with pytest.raises(CalendarError):
        execute_meeting_create(
            conn, cfg, preview_id=successor["preview_id"], runner=transport
        )
    replay = prepare_meeting_successor(
        conn,
        cfg,
        attempt_id=attempt["attempt_id"],
        expected_revision=2,
        control_message=OWNER,
    )
    assert replay["replayed"] and replay["preview_id"] == successor["preview_id"]
    second.close()


def test_dispatched_timeout_never_becomes_absent_or_new_approval(conn, config):
    cfg, transport, _, preview, attempt = uncertain(conn, config)
    transport.pages = [{"items": [], "has_more": False}]
    result = check(conn, cfg, transport, preview)
    assert result["classification"] == "inconclusive"
    assert result["reason"] == "no_match_does_not_prove_absence"
    revision = meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])[
        "revision"
    ]
    with pytest.raises(MeetingRecoveryError):
        cancel_before_dispatch(
            conn,
            cfg,
            attempt_id=attempt["attempt_id"],
            expected_revision=revision,
            control_message=OWNER,
        )
    with pytest.raises(MeetingRecoveryError):
        prepare_meeting_successor(
            conn,
            cfg,
            attempt_id=attempt["attempt_id"],
            expected_revision=revision,
            control_message=OWNER,
        )


def test_exact_unique_candidate_can_be_adopted_but_is_not_fabricated_receipt(
    conn, config
):
    cfg, transport, _, preview, _ = uncertain(conn, config)
    result = check(conn, cfg, transport, preview)
    assert result["classification"] == "unique_candidate"
    bound = bind_existing_meeting(
        conn,
        cfg,
        observation_id=result["observation_id"],
        expected_digest=result["observation_digest"],
        control_message=OWNER,
        reader=CalendarReader(transport),
        adopt=True,
    )
    assert bound["operation"] == "adopted" and bound["external_writes"] is False
    assert bound["original_outcome_uncertain"] is True
    rows = conn.execute(
        "SELECT kind,payload_json FROM meeting_recovery_observations"
    ).fetchall()
    assert not any(row["kind"] == "event_receipt" for row in rows)
    assert (
        json.loads(
            next(row["payload_json"] for row in rows if row["kind"] == "binding")
        )["provenance"]
        == "owner_adopted_existing_event"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", "changed"),
        ("visibility", "public"),
        ("recurrence", "FREQ=DAILY"),
        ("start_time", {"timestamp": "0", "timezone": "UTC"}),
        ("attendee_ability", "none"),
        ("status", "cancelled"),
        ("is_exception", True),
    ],
)
def test_similar_event_is_not_exact(conn, config, field, value):
    cfg, transport, _, preview, _ = uncertain(conn, config)
    transport.overrides[field] = value
    result = check(conn, cfg, transport, preview)
    assert result["classification"] == "partial_match"
    with pytest.raises(MeetingRecoveryError):
        bind_existing_meeting(
            conn,
            cfg,
            observation_id=result["observation_id"],
            expected_digest=result["observation_digest"],
            control_message=OWNER,
            reader=CalendarReader(transport),
        )


@pytest.mark.parametrize(
    "problem",
    [
        "missing_token",
        "repeat_token",
        "budget",
        "identity",
        "get_error",
        "attendee_error",
        "missing_attendee",
        "extra_attendee",
        "multiple",
    ],
)
def test_recovery_negative_paths_never_offer_absence(conn, config, problem):
    cfg, transport, _, preview, _ = uncertain(conn, config)
    if problem == "missing_token":
        transport.pages = [{"items": [], "has_more": True}]
    elif problem == "repeat_token":
        transport.pages = [{"items": [], "has_more": True, "page_token": "0"}]
    elif problem == "budget":
        transport.pages = [
            {"items": [], "has_more": True, "page_token": "1"},
            {"items": [], "has_more": False},
        ]
    elif problem == "identity":
        transport.identity = "bot"
    elif problem == "get_error":
        transport.fail = "calendar events get"
    elif problem == "attendee_error":
        transport.fail = "calendar event.attendees list"
    elif problem == "missing_attendee":
        transport.attendee_pages = [{"items": [], "has_more": False}]
    elif problem == "extra_attendee":
        transport.attendee_pages[0]["items"].append(
            {"type": "user", "user_id": "ou_unapproved"}
        )
    else:
        transport.pages[0]["items"].append(
            {"summary": "恢复测试", "event_id": "second_0"}
        )
    result = check(
        conn,
        cfg,
        transport,
        preview,
        budgets=RecoveryBudget(max_pages=1) if problem == "budget" else None,
    )
    assert result["classification"] not in {
        "known_created",
        "unique_candidate",
        "proven_not_dispatched",
    }
    assert (
        meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])[
            "can_repreview"
        ]
        is False
    )


def test_empty_first_page_not_absence_attendees_paginated(conn, config):
    cfg, transport, _, preview, _ = uncertain(conn, config)
    transport.pages.insert(0, {"items": [], "has_more": True, "page_token": "1"})
    transport.attendee_pages.insert(
        0, {"items": [], "has_more": True, "page_token": "1"}
    )
    result = check(conn, cfg, transport, preview)
    assert (
        result["classification"] == "unique_candidate" and result["pagination_complete"]
    )
    assert all(
        value is not None and value <= 20
        for value in transport.timeouts[-len(transport.calls) :]
    )


def test_bind_requires_fresh_read_and_exact_owner(conn, config):
    cfg, transport, _, preview, _ = uncertain(conn, config)
    result = check(conn, cfg, transport, preview)
    with pytest.raises(ApprovalError):
        bind_existing_meeting(
            conn,
            cfg,
            observation_id=result["observation_id"],
            expected_digest=result["observation_digest"],
            control_message=ControlMessage("intruder", "owner-chat", "fake", "bind"),
            reader=CalendarReader(transport),
            adopt=True,
        )
    transport.overrides["description"] = "changed after preview"
    with pytest.raises(MeetingRecoveryError, match="changed"):
        bind_existing_meeting(
            conn,
            cfg,
            observation_id=result["observation_id"],
            expected_digest=result["observation_digest"],
            control_message=OWNER,
            reader=CalendarReader(transport),
            adopt=True,
        )


def test_late_receipt_independently_durable_and_not_overwritten(conn, config):
    _cfg, transport, _, preview, attempt = uncertain(conn, config)
    record_event_receipt(
        conn,
        attempt_id=attempt["attempt_id"],
        dispatch_token=attempt["dispatch_token"],
        result=CommandResult({"event": transport.event()}, "user", []),
    )
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE meeting_previews SET status='failed' WHERE preview_id=?",
        (preview["preview_id"],),
    )
    conn.execute("ROLLBACK")
    record_failure(conn, attempt_id=attempt["attempt_id"], error=TimeoutError())
    assert (
        conn.execute("SELECT event_id FROM meeting_create_attempts").fetchone()[0]
        == transport.event_id
    )
    with pytest.raises(sqlite3.IntegrityError, match="append only"):
        conn.execute(
            "UPDATE meeting_recovery_observations SET payload_json='{}' WHERE kind='event_receipt'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="append only"):
        conn.execute(
            "DELETE FROM meeting_recovery_observations WHERE kind='event_receipt'"
        )


def test_new_identity_mismatch_before_dispatch_has_no_remote_write(conn, config):
    cfg, transport, _, preview = prepared(conn, config)
    transport.owner = "ou_someone_else"
    with pytest.raises(CalendarError):
        execute_meeting_create(
            conn, cfg, preview_id=preview["preview_id"], runner=transport
        )
    assert (
        conn.execute("SELECT phase FROM meeting_create_attempts").fetchone()[0]
        == "prepared"
    )
    assert not any(argv[2] == "create" for argv in transport.calls)


def test_legacy_preview_cannot_execute_and_history_can_be_read_after_close(
    conn, config
):
    cfg, transport, action, preview = prepared(conn, config, legacy=True)
    with pytest.raises(CalendarError, match="legacy"):
        execute_meeting_create(
            conn, cfg, preview_id=preview["preview_id"], runner=transport
        )
    conn.execute(
        "UPDATE cases SET state='resolved',lifecycle_round=lifecycle_round+1 WHERE case_id=?",
        (action["case_id"],),
    )
    assert historical_meeting_preview(conn, preview["preview_id"])[1] == action
    with pytest.raises(ApprovalError):
        exact_meeting_preview(conn, preview["preview_id"])
    before = conn.total_changes
    assert meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])[
        "read_only"
    ]
    assert conn.total_changes == before


def test_migration_keeps_unknown_legacy_target_without_network(tmp_path, config):
    conn = connect(tmp_path / "legacy-calendar.db")
    for version, name, sql in migration_files():
        if version >= 32:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (version, name, iso_now()),
        )
    cfg, transport, _, preview = prepared(conn, config, legacy=True)
    conn.execute(
        "UPDATE meeting_previews SET status='creating' WHERE preview_id=?",
        (preview["preview_id"],),
    )
    migrate(conn)
    row = conn.execute("SELECT * FROM meeting_create_attempts").fetchone()
    assert row["phase"] == "legacy_uncertain" and row["target_json"] is None
    result = check(conn, cfg, transport, preview)
    assert result["reason"] == "original_target_unknown" and transport.calls == []
    conn.close()


@pytest.mark.parametrize("change", ["owner", "calendar", "bot"])
def test_target_binding_rejects_unverified_identity(config, change):
    cfg, transport = configured(config), CalendarFixture()
    if change == "owner":
        transport.owner = "ou_other"
    elif change == "calendar":
        transport.calendar_id = "primary"
    else:
        transport.identity = "bot"
    with pytest.raises(MeetingRecoveryError):
        resolve_calendar_target(cfg, runner=transport)


def test_canonical_side_effect_change_rejected_even_if_preview_and_approval_redigested(
    conn, config
):
    cfg, transport, action, preview = prepared(conn, config)
    action["create_body"]["visibility"] = "public"
    conn.execute(
        "UPDATE meeting_previews SET action_json=?,action_digest=? WHERE preview_id=?",
        (canonical_json(action), digest(action), preview["preview_id"]),
    )
    conn.execute(
        "UPDATE approvals SET requested_action_json=?,action_digest=? WHERE approval_id=?",
        (canonical_json(action), digest(action), preview["approval_id"]),
    )
    with pytest.raises(CalendarError):
        execute_meeting_create(
            conn, cfg, preview_id=preview["preview_id"], runner=transport
        )


def legacy_uncertain(conn, config):
    cfg, transport, action, preview = prepared(conn, config, legacy=True)
    attempt_id = "mat_legacy_" + preview["preview_id"]
    conn.execute(
        "INSERT INTO meeting_create_attempts(attempt_id,preview_id,case_id,lifecycle_round,action_digest,action_json,phase,dispatch_token,created_at,updated_at) VALUES(?,?,?,1,?,?,'legacy_uncertain',?,?,?)",
        (
            attempt_id,
            preview["preview_id"],
            action["case_id"],
            preview["action_digest"],
            canonical_json(action),
            "legacy_" + preview["preview_id"],
            iso_now(),
            iso_now(),
        ),
    )
    conn.execute(
        "UPDATE meeting_previews SET status='creating' WHERE preview_id=?",
        (preview["preview_id"],),
    )
    hint = {
        "calendar_id": transport.calendar_id,
        "actor_open_id": "ou_owner",
        "identity": "user",
        "user_id_type": "open_id",
    }
    return cfg, transport, action, preview, attempt_id, hint


def test_legacy_explicit_owner_scope_can_adopt_but_never_reconstruct_origin(
    conn, config
):
    cfg, transport, action, preview, attempt_id, hint = legacy_uncertain(conn, config)
    with pytest.raises(MeetingRecoveryError, match="owner"):
        check_meeting_creation(
            conn,
            cfg,
            preview_id=preview["preview_id"],
            reader=CalendarReader(transport),
            target_hint=hint,
        )
    result = check_meeting_creation(
        conn,
        cfg,
        preview_id=preview["preview_id"],
        reader=CalendarReader(transport),
        target_hint=hint,
        control_message=OWNER,
    )
    assert result["classification"] == "legacy_candidate"
    assert result["target_basis"] == "operator_search_scope"
    assert result["candidates"][0]["historical_fields_missing"]
    with pytest.raises(MeetingRecoveryError):
        bind_existing_meeting(
            conn,
            cfg,
            observation_id=result["observation_id"],
            expected_digest=result["observation_digest"],
            control_message=OWNER,
            reader=CalendarReader(transport),
        )
    adopted = bind_existing_meeting(
        conn,
        cfg,
        observation_id=result["observation_id"],
        expected_digest=result["observation_digest"],
        control_message=OWNER,
        reader=CalendarReader(transport),
        adopt=True,
    )
    assert adopted["operation"] == "adopted" and adopted["original_outcome_uncertain"]
    row = conn.execute(
        "SELECT * FROM meeting_create_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    assert row["target_json"] is None and json.loads(row["adopted_target_json"]) == hint
    assert not conn.execute(
        "SELECT 1 FROM meeting_recovery_observations WHERE kind='event_receipt'"
    ).fetchone()
    with pytest.raises(CalendarError):
        create_meeting_preview(conn, action={**action, "summary": "another invitation"})


@pytest.mark.parametrize("same_event", [True, False])
def test_adoption_keeps_uncertain_interlock_until_late_receipt(
    conn, config, same_event
):
    cfg, transport, action, preview, attempt = uncertain(conn, config)
    result = check(conn, cfg, transport, preview)
    bind_existing_meeting(
        conn,
        cfg,
        observation_id=result["observation_id"],
        expected_digest=result["observation_digest"],
        control_message=OWNER,
        reader=CalendarReader(transport),
        adopt=True,
    )
    with pytest.raises(CalendarError):
        create_meeting_preview(
            conn, action=revise_bound_action({**action, "summary": "new invitation"})
        )
    receipt = CommandResult(
        {
            "event": transport.event()
            if same_event
            else transport.event("late_different_0")
        },
        "user",
        [],
    )
    if same_event:
        record_event_receipt(
            conn,
            attempt_id=attempt["attempt_id"],
            dispatch_token=attempt["dispatch_token"],
            result=receipt,
        )
        assert (
            meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])[
                "phase"
            ]
            == "partial"
        )
        verified = check(conn, cfg, transport, preview)
        assert verified["classification"] == "known_created"
        assert (
            bind_existing_meeting(
                conn,
                cfg,
                observation_id=verified["observation_id"],
                expected_digest=verified["observation_digest"],
                control_message=OWNER,
                reader=CalendarReader(transport),
            )["operation"]
            == "linked"
        )
    else:
        with pytest.raises(MeetingRecoveryError, match="conflicting"):
            record_event_receipt(
                conn,
                attempt_id=attempt["attempt_id"],
                dispatch_token=attempt["dispatch_token"],
                result=receipt,
            )
        report = meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])
        assert (
            report["phase"] == "conflict" and report["event_id"] == transport.event_id
        )
        assert any(
            item["payload"].get("event", {}).get("event_id") == "late_different_0"
            for item in report["observations"]
        )


def test_late_invitation_receipt_is_retained_after_timeout(conn, config):
    cfg, transport, _, preview = prepared(conn, config)
    transport.fail = "calendar event.attendees create"
    with pytest.raises(CalendarError):
        execute_meeting_create(
            conn, cfg, preview_id=preview["preview_id"], runner=transport
        )
    attempt = dict(conn.execute("SELECT * FROM meeting_create_attempts").fetchone())
    result = finish_invitation(
        conn,
        attempt_id=attempt["attempt_id"],
        dispatch_token=attempt["dispatch_token"],
        result=CommandResult(
            {"attendees": transport.attendee_pages[0]["items"]}, "user", []
        ),
    )
    assert result["outcome"] == "complete"
    assert (
        conn.execute(
            "SELECT count(*) FROM meeting_recovery_observations WHERE kind='attendee_receipt'"
        ).fetchone()[0]
        == 1
    )
    with pytest.raises(MeetingRecoveryError):
        enter_dispatch(
            conn,
            cfg,
            attempt_id=attempt["attempt_id"],
            dispatch_token=attempt["dispatch_token"],
            invitation=True,
        )


def test_attempt_change_during_scan_rejects_stale_observation(conn, config):
    cfg, transport, _, preview, attempt = uncertain(conn, config)

    def late(argv):
        if argv[:3] == ["calendar", "events", "get"]:
            record_event_receipt(
                conn,
                attempt_id=attempt["attempt_id"],
                dispatch_token=attempt["dispatch_token"],
                result=CommandResult({"event": transport.event()}, "user", []),
            )

    transport.hook = late
    result = check(conn, cfg, transport, preview)
    assert (
        result["classification"] == "inconclusive"
        and result["reason"] == "attempt_changed_during_check"
    )


def test_deadline_is_forwarded_to_each_read_and_stops_future_reads(conn, config):
    cfg, transport, _, preview, _ = uncertain(conn, config)
    elapsed = [0.0]

    def delayed(argv):
        elapsed[0] += 21.0

    transport.hook = delayed
    reader = CalendarReader(transport, monotonic=lambda: elapsed[0])
    transport.calls.clear()
    result = check_meeting_creation(
        conn,
        cfg,
        preview_id=preview["preview_id"],
        reader=reader,
        budgets=RecoveryBudget(deadline_seconds=20),
    )
    assert result["classification"] == "inconclusive" and "deadline" in result["reason"]
    assert len(transport.calls) == 1 and transport.timeouts[-1] == 20


def test_historical_past_meeting_integrity_does_not_regrant_create(conn, config):
    cfg, _, action, preview = prepared(conn, config, legacy=True)
    action["start"], action["end"] = (
        "2020-01-01T10:00:00+05:45",
        "2020-01-01T10:30:00+05:45",
    )
    conn.execute(
        "UPDATE meeting_previews SET action_json=?,action_digest=? WHERE preview_id=?",
        (canonical_json(action), digest(action), preview["preview_id"]),
    )
    conn.execute(
        "UPDATE approvals SET requested_action_json=?,action_digest=? WHERE approval_id=?",
        (canonical_json(action), digest(action), preview["approval_id"]),
    )
    assert historical_meeting_preview(conn, preview["preview_id"])[1] == action
    assert meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])[
        "read_only"
    ]
    with pytest.raises(ApprovalError, match="past"):
        exact_meeting_preview(conn, preview["preview_id"])


def test_foreign_receipt_is_retained_but_never_authorizes_invitation(conn, config):
    cfg, transport, _, preview = prepared(conn, config)
    transport.overrides["organizer_calendar_id"] = "wrong_calendar@example.invalid"
    with pytest.raises(CalendarError):
        execute_meeting_create(
            conn, cfg, preview_id=preview["preview_id"], runner=transport
        )
    report = meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])
    assert report["phase"] == "conflict" and any(
        item["kind"] == "event_receipt" for item in report["observations"]
    )
    assert not any(
        argv[:3] == ["calendar", "event.attendees", "create"]
        for argv in transport.calls
    )


def test_atomic_cancel_and_adoption_double_click_are_idempotent(conn, config):
    cfg, _, _, preview = prepared(conn, config)
    attempt = begin_attempt(conn, cfg, preview["preview_id"])
    cancel_before_dispatch(
        conn,
        cfg,
        attempt_id=attempt["attempt_id"],
        expected_revision=1,
        control_message=OWNER,
    )
    before = conn.total_changes
    assert cancel_before_dispatch(
        conn,
        cfg,
        attempt_id=attempt["attempt_id"],
        expected_revision=1,
        control_message=OWNER,
    )["replayed"]
    assert conn.total_changes == before
    cfg, transport, _, preview, _ = uncertain(conn, config)
    evidence = check(conn, cfg, transport, preview)
    kwargs = {
        "observation_id": evidence["observation_id"],
        "expected_digest": evidence["observation_digest"],
        "control_message": OWNER,
        "reader": CalendarReader(transport),
        "adopt": True,
    }
    bind_existing_meeting(conn, cfg, **kwargs)
    before, calls = conn.total_changes, len(transport.calls)
    assert bind_existing_meeting(conn, cfg, **kwargs)["replayed"]
    assert conn.total_changes == before and len(transport.calls) == calls


def test_duplicate_receipt_does_not_invalidate_displayed_observation(conn, config):
    cfg, transport, _, preview, attempt = uncertain(conn, config)
    receipt = CommandResult({"event": transport.event()}, "user", [])
    record_event_receipt(
        conn,
        attempt_id=attempt["attempt_id"],
        dispatch_token=attempt["dispatch_token"],
        result=receipt,
    )
    evidence = check(conn, cfg, transport, preview)
    before = conn.total_changes
    record_event_receipt(
        conn,
        attempt_id=attempt["attempt_id"],
        dispatch_token=attempt["dispatch_token"],
        result=receipt,
    )
    assert conn.total_changes == before
    assert (
        bind_existing_meeting(
            conn,
            cfg,
            observation_id=evidence["observation_id"],
            expected_digest=evidence["observation_digest"],
            control_message=OWNER,
            reader=CalendarReader(transport),
        )["operation"]
        == "linked"
    )


def test_read_original_calendar_when_current_primary_changed(conn, config):
    cfg, transport, action, preview, _ = uncertain(conn, config)
    original = action["target"]["calendar_id"]
    transport.calendar_id = "new_primary@example.invalid"
    transport.overrides["organizer_calendar_id"] = original
    result = check(conn, cfg, transport, preview)
    assert result["classification"] == "unique_candidate"
    get = next(
        argv for argv in transport.calls if argv[:3] == ["calendar", "events", "get"]
    )
    assert get[get.index("--calendar-id") + 1] == original


def test_recovery_card_is_read_only_paginated_and_never_offers_unknown_retry(
    conn, config
):
    from k3_support.meeting_preview import recovery_panel

    cfg, transport, _, preview, _ = uncertain(conn, config)
    check(conn, cfg, transport, preview)
    report = meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])
    before = conn.total_changes
    cards = [recovery_panel(report)]
    cards.extend(
        recovery_panel(report, page=page)
        for page in range(2, cards[0]["preview"]["page_count"] + 1)
    )
    buttons = [button for card in cards for button in card["preview"]["buttons"]]
    assert conn.total_changes == before
    assert any(button["callback_data"].startswith("mr:a:") for button in buttons)
    assert not any(
        button["callback_data"].startswith(("mr:b:", "mr:n:", "mr:x:"))
        for button in buttons
    )
    assert all(len(button["callback_data"].encode()) <= 64 for button in buttons)
    assert all(len(card["preview"]["text"]) < 4096 for card in cards)
    assert "原" in "".join(card["preview"]["text"] for card in cards)


def test_recovery_report_history_bound_has_honest_total(conn, config, monkeypatch):
    cfg, transport, _, preview, _ = uncertain(conn, config)
    for index in range(4):
        # Identical observations in the same millisecond intentionally dedupe.
        # Give these four distinct fixture checks explicit distinct timestamps.
        monkeypatch.setattr(
            "k3_support.meeting_recovery.iso_now",
            lambda i=index: f"2026-09-07T10:00:0{i}+00:00",
        )
        check(conn, cfg, transport, preview)
    report = meeting_recovery_report(
        conn, cfg, preview_id=preview["preview_id"], observation_limit=2
    )
    assert len(report["observations"]) == 2 and report["observation_total"] == 5
    assert report["observations"][0]["kind"] == "check"


@pytest.mark.parametrize("field", ["action_json", "target_json", "dispatch_token"])
def test_attempt_identity_cannot_be_reassigned(conn, config, field):
    cfg, _, _, preview = prepared(conn, config)
    begin_attempt(conn, cfg, preview["preview_id"])
    replacement = "{}" if field.endswith("json") else "fake"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(f"UPDATE meeting_create_attempts SET {field}=?", (replacement,))


def test_new_time_preview_rebuilds_actual_request_and_operation_key(conn, config):
    _cfg, _, action, preview = prepared(conn, config)
    revised = revise_bound_action(
        {
            **action,
            "start": "2028-01-05T15:00:00+05:45",
            "end": "2028-01-05T15:30:00+05:45",
        }
    )
    assert revised["operation_id"] != action["operation_id"]
    assert (
        int(revised["create_body"]["start_time"]["timestamp"])
        - int(action["create_body"]["start_time"]["timestamp"])
        == 3600
    )
    assert revised["target"] == action["target"]
    assert exact_meeting_preview(conn, preview["preview_id"])[1] == action


def test_unexecuted_legacy_repreview_revokes_old_and_needs_new_exact_approval(
    conn, config
):
    cfg, transport, _, preview = prepared(conn, config, legacy=True)
    successor = prepare_legacy_meeting_preview(
        conn,
        cfg,
        preview_id=preview["preview_id"],
        expected_digest=preview["action_digest"],
        control_message=OWNER,
        runner=transport,
    )
    assert successor["action"]["schema_version"] == 2 and successor["requires_approval"]
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?",
            (preview["approval_id"],),
        ).fetchone()[0]
        == "revoked"
    )
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?",
            (successor["approval_id"],),
        ).fetchone()[0]
        == "requested"
    )
    assert not any(argv[2] == "create" for argv in transport.calls)
    with pytest.raises(CalendarError):
        execute_meeting_create(
            conn, cfg, preview_id=successor["preview_id"], runner=transport
        )
    calls = len(transport.calls)
    replay = prepare_legacy_meeting_preview(
        conn,
        cfg,
        preview_id=preview["preview_id"],
        expected_digest=preview["action_digest"],
        control_message=OWNER,
        runner=transport,
    )
    assert replay["preview_id"] == successor["preview_id"] and replay["replayed"]
    assert len(transport.calls) == calls


def test_legacy_uncertain_cannot_use_repreview_shortcut(conn, config):
    cfg, transport, _, preview, _, _ = legacy_uncertain(conn, config)
    with pytest.raises(MeetingRecoveryError, match="uncertain"):
        prepare_legacy_meeting_preview(
            conn,
            cfg,
            preview_id=preview["preview_id"],
            expected_digest=preview["action_digest"],
            control_message=OWNER,
            runner=transport,
        )


def test_legacy_old_process_consumption_during_lookup_blocks_new_approval(conn, config):
    cfg, transport, _, preview = prepared(conn, config, legacy=True)

    def old_dispatch(argv):
        if argv[:3] == ["calendar", "calendars", "primary"]:
            conn.execute(
                "UPDATE approvals SET status='consumed',consumed_at=? WHERE approval_id=?",
                (iso_now(), preview["approval_id"]),
            )
            conn.execute(
                "UPDATE meeting_previews SET status='creating' WHERE preview_id=?",
                (preview["preview_id"],),
            )

    transport.hook = old_dispatch
    with pytest.raises(MeetingRecoveryError, match="consumed"):
        prepare_legacy_meeting_preview(
            conn,
            cfg,
            preview_id=preview["preview_id"],
            expected_digest=preview["action_digest"],
            control_message=OWNER,
            runner=transport,
        )
    assert conn.execute("SELECT count(*) FROM meeting_previews").fetchone()[0] == 1
    before = conn.total_changes
    assert (
        meeting_recovery_report(conn, cfg, preview_id=preview["preview_id"])["phase"]
        == "legacy_uncertain"
    )
    assert conn.total_changes == before
    transport.hook = None
    result = check(conn, cfg, transport, preview)
    assert result["reason"] == "original_target_unknown" and transport.calls == []
    assert (
        conn.execute("SELECT phase FROM meeting_create_attempts").fetchone()[0]
        == "legacy_uncertain"
    )


def test_recovery_refuses_mutated_preview_even_with_fresh_approval_digest(conn, config):
    cfg, transport, action, preview, _ = uncertain(conn, config)
    changed = revise_bound_action({**action, "summary": "changed"})
    conn.execute(
        "UPDATE meeting_previews SET action_json=?,action_digest=? WHERE preview_id=?",
        (canonical_json(changed), digest(changed), preview["preview_id"]),
    )
    conn.execute(
        "UPDATE approvals SET requested_action_json=?,action_digest=? WHERE approval_id=?",
        (canonical_json(changed), digest(changed), preview["approval_id"]),
    )
    with pytest.raises(MeetingRecoveryError, match="immutable"):
        check(conn, cfg, transport, preview)


@pytest.mark.parametrize(
    ("requested", "returned", "complete", "booked"),
    [
        (["ou_peer", "omm_room"], [("user", "ou_peer", "needs_action")], False, False),
        (
            ["ou_peer"],
            [
                ("user", "ou_peer", "needs_action"),
                ("user", "ou_unapproved", "needs_action"),
            ],
            False,
            None,
        ),
        (
            ["ou_peer"],
            [("user", "ou_peer", "needs_action"), ("user", "ou_owner", "accept")],
            True,
            None,
        ),
        (["ou_peer"], [("user", "ou_peer", "needs_action")], True, None),
        (["omm_room"], [("resource", "omm_room", "accept")], True, True),
        (
            ["omm_room"],
            [("resource", "omm_room", "accept"), ("resource", "omm_room", "accept")],
            True,
            True,
        ),
        (["omm_room", "omm_other"], [("resource", "omm_room", "accept")], False, False),
        (
            ["ou_peer"],
            [
                ("user", "ou_peer", "needs_action"),
                ("resource", "omm_unapproved", "accept"),
            ],
            False,
            None,
        ),
    ],
)
def test_invitation_receipt_and_readback_require_exact_membership_and_expected_rooms(
    conn, config, requested, returned, complete, booked
):
    cfg, transport, _, preview = prepared(conn, config, attendees=requested)
    fields = {"user": "user_id", "resource": "room_id"}
    transport.attendee_pages = [
        {
            "items": [
                {"type": kind, fields[kind]: identity, "rsvp_status": state}
                for kind, identity, state in returned
            ],
            "has_more": False,
        }
    ]
    result = execute_meeting_create(
        conn, cfg, preview_id=preview["preview_id"], runner=transport
    )
    assert result["invitation_membership_confirmed"] is complete
    assert result["outcome"] == ("complete" if complete else "partial")
    assert result["rooms_booked"] is booked
    assert result["event_id"] == transport.event_id
    observation = check(conn, cfg, transport, preview)
    assert observation["candidates"][0]["exact"] is complete
    assert observation["candidates"][0]["rooms_booked"] is booked


@pytest.mark.parametrize(
    "states",
    [
        ("decline", "accept"),
        ("accept", "decline"),
        ("removed", "accept"),
        ("accept", "removed"),
    ],
)
def test_conflicting_duplicate_room_status_is_not_resolved_by_record_order(
    conn, config, states
):
    cfg, transport, _, preview = prepared(conn, config, attendees=["omm_room"])
    transport.attendee_pages = [
        {
            "items": [
                {"type": "resource", "room_id": "omm_room", "rsvp_status": state}
                for state in states
            ],
            "has_more": False,
        }
    ]
    result = execute_meeting_create(
        conn, cfg, preview_id=preview["preview_id"], runner=transport
    )
    assert (
        result["outcome"] == "partial" and not result["invitation_membership_confirmed"]
    )
    assert result["rooms_booked"] is False
    assert result["event_id"] == transport.event_id
    assert (
        conn.execute(
            "SELECT count(*) FROM meeting_recovery_observations WHERE kind='attendee_receipt'"
        ).fetchone()[0]
        == 1
    )
    observation = check(conn, cfg, transport, preview)
    assert observation["classification"] == "inconclusive"
    assert not observation["candidates"]
