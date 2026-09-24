"""Synthetic conversation invariants; no live IM or model is contacted."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

import pytest
from test_inbound_claims import _guard

from k3_support import conversation_context as context
from k3_support.coordination import (
    bind_ai_communication,
    control_communication,
    ensure_turn,
    validate_outbox_fence,
)
from k3_support.db import connect, transaction
from k3_support.ids import digest
from k3_support.inbound_claims import (
    InboundContextSuperseded,
    claim_events,
    supersede_claim,
)
from k3_support.ingress import poll_anchored_threads
from k3_support.lark import CommandResult
from k3_support.store import create_case, enqueue_outbox, ingest_event


def settings(config):
    config.raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    config.raw["scope"]["technical_chat_ids"] = ["oc_group"]
    return config


def item(
    number=1,
    *,
    content="Pico 风扇怎么调",
    group=True,
    parent=None,
    sender="ou_peer",
    hour=None,
    at=None,
):
    payload = {
        "content": content,
        "chat_type": "group" if group else "p2p",
        "sender_type": "user",
        "mentions": [],
    }
    # Default seed @; follow-ups have only a previously observed anchor.
    payload["mentions"] = (
        [{"id": "ou_owner"}]
        if (at if at is not None else number == 1 and group)
        else []
    )
    if parent:
        payload["root_id"] = parent
    return {
        "source": "feishu_user_poll",
        "identity": "user",
        "external_id": f"om_context_{number}",
        "chat_id": "oc_group" if group else "oc_peer",
        "sender_id": sender,
        "thread_id": parent,
        "occurred_at": f"2026-09-07T{hour if hour is not None else number:02}:00:00+00:00",
        "payload": payload,
    }


def admitted(conn, config, value):
    key, created = context.admit_im_event(conn, settings(config), value)
    assert key and created
    return key, context.resolve_event_context(conn, key)


def case(conn, key):
    row = conn.execute(
        "SELECT * FROM inbound_events WHERE event_pk=?", (key,)
    ).fetchone()
    return create_case(
        conn,
        title="Synthetic case",
        case_type="faq",
        severity="P3",
        confidence=0.99,
        requester_id=row["sender_id"],
        requester_chat_id=row["chat_id"],
        source_event_pk=key,
    )[0]


def reply(conn, config, key, cid):
    with transaction(conn):
        ensure_turn(conn, case_id=cid, source_event_pk=key)
        binding = bind_ai_communication(conn, config, case_id=cid, source_event_pk=key)
        assert binding
        oid, _ = enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="reply",
            destination="om_reply",
            payload={"text": "synthetic only"},
            idempotency_key="test:" + key,
            case_id=cid,
            source_event_pk=key,
            **{
                field: binding[field]
                for field in (
                    "turn_id",
                    "turn_revision",
                    "communication_fence",
                    "context_id",
                    "context_revision",
                    "context_digest",
                )
            },
        )
    return dict(
        conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (oid,)).fetchone()
    )


def test_group_anchor_before_case_accepts_other_sender_after_six_hours(conn, config):
    first, seed = admitted(conn, config, item())
    assert seed["case_id"] is None
    second, follow = admitted(
        conn,
        config,
        item(
            2,
            sender="ou_test",
            hour=7,
            parent="om_context_1",
            content="现在 EVB，之前说错了",
        ),
    )
    assert follow["context_id"] == seed["context_id"] and follow["revision"] == 2
    cid = case(conn, first)
    result = context.bind_context_case(conn, seed["context_id"], cid)
    assert (
        result["source_event_pks"] == [first, second]
        and "Pico" in result["query"]
        and "EVB" in result["query"]
    )
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 1
    assert (
        result["facts"]["fields"]["board"]["state"] == "conflict"
    )  # another author's unexplained conflicting site is not authority


@pytest.mark.parametrize("variant", ["chat", "thread", "unanchored", "application"])
def test_group_followup_scope_does_not_expand(conn, config, variant):
    admitted(conn, config, item())
    value = item(2, parent="om_context_1")
    if variant == "chat":
        value["chat_id"] = "oc_other"
    elif variant == "thread":
        value["thread_id"] = value["payload"]["root_id"] = "omt_other"
    elif variant == "unanchored":
        value["thread_id"] = None
        value["payload"].pop("root_id")
    else:
        value["payload"]["sender_type"] = "app"
    assert context.admit_im_event(conn, config, value) == (None, False)
    assert conn.execute("SELECT count(*) FROM inbound_events").fetchone()[0] == 1


def test_cross_ingress_concurrent_duplicate_has_one_member_and_revision(conn, config):
    settings(config)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def ingest(source, identity):
        own = connect(config.database_path)
        try:
            value = item()
            value.update(source=source, identity=identity)
            barrier.wait(timeout=5)
            results.append(context.admit_im_event(own, config, value))
        except Exception as exc:  # noqa: BLE001 - report thread failures to the asserting test
            errors.append(exc)
        finally:
            own.close()

    threads = [
        threading.Thread(target=ingest, args=args)
        for args in [("feishu_user_poll", "user"), ("feishu_bot_im", "bot")]
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert sorted(created for _, created in results) == [False, True]
    assert len({key for key, _ in results}) == 1
    assert conn.execute("SELECT revision FROM conversation_contexts").fetchone()[0] == 1
    assert (
        conn.execute("SELECT count(*) FROM conversation_context_members").fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("changed", ["content", "thread"])
def test_conflicting_replay_retains_variant_and_cannot_grant_alias(
    conn, config, changed
):
    key, seed = admitted(conn, config, item(parent="omt_actual"))
    before = conn.execute(
        "SELECT payload_json FROM inbound_events WHERE event_pk=?", (key,)
    ).fetchone()[0]
    value = item(parent="omt_actual")
    value.update(source="feishu_bot_im", identity="bot")
    if changed == "content":
        value["payload"]["content"] = "Contradictory EVB body"
    value["thread_id"] = value["payload"]["root_id"] = "omt_unrelated"
    assert context.admit_im_event(conn, config, value) == (key, False)
    current = context.project_context(conn, seed["context_id"])
    assert (
        current["state"] == "conflict" and current["conflicts"][0]["variant"] == value
    )
    assert (
        conn.execute(
            "SELECT payload_json FROM inbound_events WHERE event_pk=?", (key,)
        ).fetchone()[0]
        == before
    )
    assert not conn.execute(
        "SELECT 1 FROM conversation_anchor_aliases WHERE alias='omt_unrelated'"
    ).fetchone()


def test_new_input_invalidates_queued_and_claimed_stamp_before_projection(conn, config):
    key, seed = admitted(conn, config, item())
    cid = case(conn, key)
    old = reply(conn, config, key, cid)
    assert validate_outbox_fence(conn, old) == (True, None)
    admitted(conn, config, item(2, parent="om_context_1", content="不是 Pico，是 EVB"))
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE outbox_id=?", (old["outbox_id"],)
        ).fetchone()[0]
        == "cancelled"
    )
    assert validate_outbox_fence(conn, old)[0] is False
    assert context.context_snapshot(conn, seed["context_id"])["state"] == "dirty"


def test_p2p_pending_association_cannot_be_washed_by_projection(conn, config):
    key, old = admitted(conn, config, item(group=False))
    cid = case(conn, key)
    draft = reply(conn, config, key, cid)
    new_key, new = admitted(
        conn, config, item(2, group=False, content="不是 Pico，是 EVB")
    )
    assert new["context_id"] != old["context_id"] and new["candidate_context_ids"] == [
        old["context_id"]
    ]
    held = context.project_context(conn, old["context_id"])
    assert (
        held["state"] == "awaiting_relation"
        and new["context_id"] in held["pending_associations"]
    )
    with pytest.raises(context.ContextError, match="explicit Case"):
        context.resolve_pending_associations(conn, new["context_id"])
    merged = context.bind_context_case(conn, new["context_id"], cid)
    assert merged["context_id"] == old["context_id"] and merged["state"] == "ready"
    assert merged["source_event_pks"] == [key, new_key]
    assert merged["facts"]["observed_scope"]["board"] == "evb"
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE outbox_id=?", (draft["outbox_id"],)
        ).fetchone()[0]
        == "cancelled"
    )


def test_p2p_new_topic_releases_hold_without_reviving_old_answer(conn, config):
    key, old = admitted(conn, config, item(group=False))
    draft = reply(conn, config, key, case(conn, key))
    new_key, new = admitted(
        conn, config, item(2, group=False, content="另一个独立问题")
    )
    context.bind_context_case(conn, new["context_id"], case(conn, new_key))
    assert context.project_context(conn, old["context_id"])["state"] == "ready"
    assert not validate_outbox_fence(conn, draft)[0]


@pytest.mark.parametrize("action", ["claim", "suggest_only"])
def test_owner_communication_choice_survives_new_turn(conn, config, action):
    key, seed = admitted(conn, config, item())
    cid = case(conn, key)
    reply(conn, config, key, cid)
    control_communication(
        conn,
        case_id=cid,
        action=action,
        actor_id="owner-user",
        external_id="owner-action",
    )
    next_key, _ = admitted(conn, config, item(2, parent="om_context_1"))
    with transaction(conn):
        next_turn = ensure_turn(conn, case_id=cid, source_event_pk=next_key)
    assert next_turn["communication_owner"] == "human"
    assert next_turn["communication_mode"] == (
        "suggest_only" if action == "suggest_only" else "silent"
    )
    assert (
        bind_ai_communication(conn, config, case_id=cid, source_event_pk=next_key)
        is None
    )
    assert (
        context.context_snapshot(conn, seed["context_id"])["communication_owner"]
        == "human"
    )


def test_owner_precedes_case_creation_and_all_new_turns(conn, config):
    key, seed = admitted(conn, config, item())
    owner_key, _ = admitted(
        conn,
        config,
        item(2, parent="om_context_1", sender="ou_owner", content="我来处理"),
    )
    assert (
        conn.execute(
            "SELECT status FROM inbound_events WHERE event_pk=?", (owner_key,)
        ).fetchone()[0]
        == "ignored"
    )
    cid = case(conn, key)
    with transaction(conn):
        turn = ensure_turn(conn, case_id=cid, source_event_pk=key)
    assert turn["communication_owner"] == "human"
    assert (
        context.project_context(conn, seed["context_id"])["communication_owner"]
        == "human"
    )


def test_late_old_message_cannot_override_current_correction_and_sources_are_bound(
    conn, config
):
    _, seed = admitted(conn, config, item())
    new_key, _ = admitted(
        conn, config, item(3, parent="om_context_1", content="更正：不是 Pico，是 EVB")
    )
    admitted(conn, config, item(2, parent="om_context_1", content="目前是 Pico"))
    result = context.project_context(conn, seed["context_id"])
    assert result["facts"]["observed_scope"]["board"] == "evb"
    assert result["focus_event_pk"] == new_key
    for mention in result["facts"]["mentions"]:
        source = mention["source"]
        assert (
            source["event_pk"] in result["source_event_pks"] and source["event_digest"]
        )
        raw = json.loads(
            conn.execute(
                "SELECT payload_json FROM inbound_events WHERE event_pk=?",
                (source["event_pk"],),
            ).fetchone()[0]
        )["content"]
        assert raw[source["start"] : source["end"]] == mention["text"]
        assert source["input_digest"] == digest(raw)


def test_reference_and_test_board_are_not_current_site_facts(conn, config):
    _, seed = admitted(conn, config, item(content="现场不是 Pico，目前是 EVB"))
    admitted(
        conn,
        config,
        item(
            2,
            parent="om_context_1",
            content="board1 使用 Pico 测试正常",
            sender="ou_owner",
        ),
    )
    result = context.project_context(conn, seed["context_id"])
    assert result["facts"]["observed_scope"]["board"] == "evb"
    assert any(m["subject"] == "test_board:board1" for m in result["facts"]["mentions"])


@pytest.mark.parametrize(
    "label,subject",
    [
        ("board1版本", "test_board:board1"),
        ("board10版本", "case_site"),
        ("aboard1版本", "case_site"),
    ],
)
def test_test_board_subject_respects_ascii_identifier_boundary_in_chinese(
    conn, config, label, subject
):
    _, seed = admitted(
        conn, config, item(content=f"{label}是v9.0，我升级到新版后启动卡住了")
    )
    result = context.project_context(conn, seed["context_id"])
    versions = [
        m for m in result["facts"]["mentions"] if m["field"] == "software_version"
    ]
    assert versions and all(m["subject"] == subject for m in versions)


def test_new_explicit_uncertainty_withdraws_old_caller_fact(conn, config):
    _, seed = admitted(conn, config, item(content="现场使用 Pico"))
    admitted(
        conn,
        config,
        item(2, parent="om_context_1", content="更正：现在不确定是不是 EVB"),
    )
    result = context.project_context(conn, seed["context_id"])
    assert "board" not in result["facts"]["observed_scope"]
    assert result["facts"]["fields"]["board"]["state"] == "unknown"


@pytest.mark.parametrize("field", ["facts_json", "query_text", "source"])
def test_context_integrity_is_readonly_and_rejects_changed_projection_or_source(
    conn, config, field
):
    key, seed = admitted(conn, config, item())
    projected = context.project_context(conn, seed["context_id"])
    if field == "source":
        conn.execute(
            "UPDATE inbound_events SET payload_json=? WHERE event_pk=?",
            (json.dumps({"content": "mutated", "chat_type": "group"}), key),
        )
    else:
        conn.execute(
            f"UPDATE conversation_contexts SET {field}=? WHERE context_id=?",
            ("{}" if field == "facts_json" else "mutated query", seed["context_id"]),
        )
    before = conn.total_changes
    assert not context.validate_context_binding(conn, projected["binding"])[0]
    assert context.context_snapshot(conn, seed["context_id"])["state"] == "conflict"
    assert conn.total_changes == before


def test_association_merge_cannot_launder_provisional_conflict(conn, config):
    key, old = admitted(conn, config, item(group=False))
    cid = case(conn, key)
    context.bind_context_case(conn, old["context_id"], cid)
    new_key, pending = admitted(
        conn, config, item(2, group=False, content="原始新内容")
    )
    variant = item(2, group=False, content="冲突的新内容")
    assert context.admit_im_event(conn, config, variant) == (new_key, False)
    result = context.bind_context_case(conn, pending["context_id"], cid)
    assert (
        result["state"] == "conflict" and result["conflicts"][0]["variant"] == variant
    )


def test_guarded_merge_maintains_fence_and_does_not_revive_pending_reply(conn, config):
    key, old = admitted(conn, config, item(group=False))
    cid = case(conn, key)
    draft = reply(conn, config, key, cid)
    next_key, pending = admitted(
        conn, config, item(2, group=False, content="不是 Pico，是 EVB")
    )
    guarded, _ = _guard(conn, next_key)
    guarded.bind_case(cid)
    result = context.bind_context_case(guarded, pending["context_id"], cid)
    assert result["context_id"] == old["context_id"]
    guarded.checkpoint()
    assert not validate_outbox_fence(conn, draft)[0]


def test_oversized_context_is_explicitly_incomplete_and_preserves_source(conn, config):
    key, seed = admitted(conn, config, item(content="Pico " + "x" * 32768))
    result = context.project_context(conn, seed["context_id"])
    assert result["state"] == "incomplete" and result["query"] == ""
    assert result["facts"]["incomplete_reason"] == "context_query_exceeds_32768"
    assert (
        len(
            json.loads(
                conn.execute(
                    "SELECT payload_json FROM inbound_events WHERE event_pk=?", (key,)
                ).fetchone()[0]
            )["content"]
        )
        > 32768
    )
    assert not context.validate_context_binding(conn, result["binding"])[0]


def test_projection_cas_does_not_overwrite_newer_ingress(conn, config, monkeypatch):
    _, seed = admitted(conn, config, item())
    original = context.project_facts

    def changed(*args, **kwargs):
        own = connect(config.database_path)
        try:
            admitted(
                own, config, item(2, parent="om_context_1", content="不是 Pico，是 EVB")
            )
        finally:
            own.close()
        return original(*args, **kwargs)

    monkeypatch.setattr(context, "project_facts", changed)
    with pytest.raises(context.ContextError, match="changed while projecting"):
        context.project_context(conn, seed["context_id"])
    assert (
        context.context_snapshot(conn, seed["context_id"])["projected_revision"] == -1
    )


@pytest.mark.parametrize(
    "boundary", ["execute", "executemany", "cursor", "checkpoint", "transaction"]
)
def test_new_input_fences_every_old_worker_write_boundary(conn, config, boundary):
    key, _ = admitted(conn, config, item())
    guarded, claim = _guard(conn, key)
    guarded.checkpoint()
    other = connect(config.database_path)
    try:
        admitted(other, config, item(2, parent="om_context_1"))
    finally:
        other.close()
    with pytest.raises(InboundContextSuperseded):
        if boundary == "execute":
            guarded.execute("UPDATE inbound_events SET last_error='late'")
        elif boundary == "executemany":
            guarded.executemany("UPDATE inbound_events SET last_error=?", [("late",)])
        elif boundary == "cursor":
            guarded.cursor().execute("UPDATE inbound_events SET last_error='late'")
        elif boundary == "checkpoint":
            guarded.checkpoint()
        else:
            with transaction(guarded):
                pass
    assert not conn.execute(
        "SELECT 1 FROM inbound_events WHERE last_error='late'"
    ).fetchone()
    assert supersede_claim(conn, claim)
    assert not supersede_claim(conn, claim)
    assert conn.execute(
        "SELECT status,last_error FROM inbound_events WHERE event_pk=?", (key,)
    ).fetchone()[:] == ("ignored", "context_superseded")


def test_superseded_terminal_is_bound_to_attempt_not_same_worker_name(conn, config):
    key, _ = admitted(conn, config, item())
    _, claim = _guard(conn, key)
    conn.execute(
        "UPDATE inbound_events SET lease_expires_at='2020-01-01T00:00:00+00:00' WHERE event_pk=?",
        (key,),
    )
    next_claim = claim_events(conn, worker_id=claim.worker_id)[0]
    assert next_claim.claim_token != claim.token
    assert not supersede_claim(conn, claim)
    assert (
        conn.execute(
            "SELECT status FROM inbound_events WHERE event_pk=?", (key,)
        ).fetchone()[0]
        == "claimed"
    )


def test_guarded_case_binding_and_projection_are_not_false_supersession(conn, config):
    settings(config)
    value = item(group=False)
    value["payload"].pop("chat_type")
    key, _ = ingest_event(conn, **value)
    guarded, _ = _guard(conn, key)
    cid = case(guarded, key)
    with transaction(guarded):
        turn = ensure_turn(guarded, case_id=cid, source_event_pk=key)
    assert turn["communication_owner"] == "ai"
    guarded.checkpoint()
    result = context.resolve_event_context(guarded, key)
    context.project_context(guarded, result["context_id"])
    guarded.checkpoint()
    with transaction(guarded):
        guarded.finish("processed")
    assert (
        conn.execute(
            "SELECT status FROM inbound_events WHERE event_pk=?", (key,)
        ).fetchone()[0]
        == "processed"
    )


def test_guarded_context_rebind_rollback_restores_prior_stamp(conn, config):
    settings(config)
    key, _ = ingest_event(conn, **item(group=False))
    guarded, _ = _guard(conn, key)
    cid = case(guarded, key)
    with pytest.raises(RuntimeError, match="abort association"), transaction(guarded):
        context.adopt_case_event(guarded, cid, key)
        raise RuntimeError("abort association")
    assert context.resolve_event_context(conn, key) is None
    guarded.checkpoint()
    with transaction(guarded):
        guarded.execute("SAVEPOINT same")
        context.adopt_case_event(guarded, cid, key)
        guarded.execute("ROLLBACK TO same")
        guarded.execute("RELEASE same")
    guarded.checkpoint()


def test_legacy_missing_context_stamp_never_becomes_sendable(conn, config):
    settings(config)
    key, _ = ingest_event(conn, **item(group=False))
    legacy = {"channel": "feishu_im", "action_type": "reply", "source_event_pk": key}
    assert validate_outbox_fence(conn, legacy) == (False, "context_binding_missing")
    cid = case(conn, key)
    with transaction(conn):
        ensure_turn(conn, case_id=cid, source_event_pk=key)
    assert not validate_outbox_fence(conn, legacy)[0]


def thread_message(number=2, *, root="om_context_1", sender="ou_other"):
    return {
        "message_id": f"om_context_{number}",
        "chat_id": "oc_group",
        "chat_type": "group",
        "thread_id": root,
        "root_id": root,
        "create_time": "2026-09-07 19:00",
        "sender": {"id": sender, "sender_type": "user"},
        "content": "现场用 EVB",
        "msg_type": "text",
    }


@pytest.mark.parametrize("fail_midway", [False, True])
def test_thread_page_prefix_is_not_visible_to_concurrent_worker(
    conn, config, monkeypatch, fail_midway
):
    from k3_support import ingress

    key, seed = admitted(conn, config, item())
    old = reply(conn, config, key, case(conn, key))
    other = connect(config.database_path)
    original = ingress.admit_im_event
    checks = []

    def intercepted(*args, **kwargs):
        result = original(*args, **kwargs)
        assert conn.in_transaction
        checks.append(
            other.execute("SELECT count(*) FROM inbound_events").fetchone()[0]
        )
        if fail_midway and len(checks) == 2:
            raise RuntimeError("synthetic page admission failure")
        return result

    def runner(_):
        assert not conn.in_transaction
        return CommandResult(
            {
                "messages": [thread_message(2), thread_message(3)],
                "meta": {
                    "pagination": {"complete": False, "next_token": "synthetic-next"}
                },
            },
            "user",
            [],
        )

    monkeypatch.setattr(ingress, "admit_im_event", intercepted)
    try:
        if fail_midway:
            with pytest.raises(RuntimeError, match="page admission failure"):
                poll_anchored_threads(
                    conn, config, now=datetime.now(UTC), runner=runner
                )
        else:
            poll_anchored_threads(conn, config, now=datetime.now(UTC), runner=runner)
        assert checks == [1, 1]
        assert other.execute("SELECT count(*) FROM inbound_events").fetchone()[0] == (
            1 if fail_midway else 3
        )
        assert (
            context.project_context(conn, seed["context_id"])["state"] == "incomplete"
        )
        assert not validate_outbox_fence(conn, old)[0]
    finally:
        other.close()


def test_thread_poll_is_bounded_resumable_and_complete_does_not_revive_reply(
    conn, config
):
    key, seed = admitted(conn, config, item())
    draft = reply(conn, config, key, case(conn, key))
    calls = []

    def partial(argv):
        calls.append(argv)
        return CommandResult(
            {
                "messages": [thread_message()],
                "meta": {
                    "pagination": {"complete": False, "next_token": "synthetic-page2"}
                },
            },
            "user",
            [],
        )

    now = datetime.now(UTC)
    assert (
        poll_anchored_threads(conn, config, now=now, runner=partial)["incomplete"] == 1
    )
    state = context.project_context(conn, seed["context_id"])
    assert (
        state["state"] == "incomplete" and state["thread_cursor"] == "synthetic-page2"
    )
    revision = state["revision"]
    poll_anchored_threads(conn, config, now=now, runner=partial)
    assert context.context_snapshot(conn, seed["context_id"])["revision"] == revision

    def complete(argv):
        calls.append(argv)
        return CommandResult(
            {"messages": [], "meta": {"pagination": {"complete": True}}}, "user", []
        )

    assert (
        poll_anchored_threads(conn, config, now=now, runner=complete)["incomplete"] == 0
    )
    assert (
        "--page-token" in calls[-1]
        and calls[-1][calls[-1].index("--page-token") + 1] == "synthetic-page2"
    )
    assert all(
        argv[argv.index("--page-limit") + 1] == "2" and "--start" not in argv
        for argv in calls
    )
    assert context.project_context(conn, seed["context_id"])["state"] == "ready"
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE outbox_id=?", (draft["outbox_id"],)
        ).fetchone()[0]
        == "cancelled"
    )


@pytest.mark.parametrize(
    "metadata",
    [{}, {"complete": True, "next_token": "still-pending"}, {"complete": False}],
)
def test_thread_pagination_missing_or_contradictory_is_not_complete(
    conn, config, metadata
):
    _, seed = admitted(conn, config, item())
    result = poll_anchored_threads(
        conn,
        config,
        now=datetime.now(UTC),
        runner=lambda _: CommandResult(
            {"messages": [], "meta": {"pagination": metadata}}, "user", []
        ),
    )
    assert (
        result["incomplete"] == 1
        and context.project_context(conn, seed["context_id"])["state"] == "incomplete"
    )


def test_thread_batch_budget_fairness_and_foreign_chat_not_admitted(conn, config):
    settings(config)
    for number in range(1, 7):
        admitted(conn, config, item(number, at=True))
    calls = []

    def runner(argv):
        calls.append(argv[argv.index("--thread") + 1])
        bad = thread_message()
        bad["chat_id"] = "oc_unrelated"
        return CommandResult(
            {"messages": [bad], "meta": {"pagination": {"complete": True}}}, "user", []
        )

    result = poll_anchored_threads(conn, config, now=datetime.now(UTC), runner=runner)
    assert result["threads"] == 4 and len(calls) == 4 and result["incomplete"] == 4
    poll_anchored_threads(conn, config, now=datetime.now(UTC), runner=runner)
    assert len(set(calls)) == 6
    assert conn.execute("SELECT count(*) FROM inbound_events").fetchone()[0] == 6
