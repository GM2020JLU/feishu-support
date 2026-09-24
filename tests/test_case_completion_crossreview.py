"""Independent A-batch counterexamples: real local UI path, synthetic inputs."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_case_completion_ui import answered_faq, configure_plugin
from test_workbench_navigation import Query

import k3_support.hermes_plugin as plugin
from k3_support.case_detail import case_detail
from k3_support.coordination import ensure_turn
from k3_support.store import ingest_event
from k3_support.workbench import workbench_snapshot


def _resolve_callback(conn, case_id):
    return next(
        button["callback_data"]
        for button in case_detail(conn, case_id=case_id)["preview"]["buttons"]
        if button["text"] == "标记解决"
    )


def _click(
    callback, *, identifier="crossreview-click", user="owner-user", chat="owner-chat"
):
    async def exercise():
        query = Query(callback)
        query.id = identifier
        query.from_user = SimpleNamespace(id=user)
        query.message = SimpleNamespace(chat_id=chat, message_id="crossreview-detail")
        await plugin._handle_workbench_callback(query, callback)
        return query

    return asyncio.run(exercise())


def test_answered_faq_remains_pending_for_another_colleague_in_anchored_group_thread(
    conn,
    config,
):
    cfg, case_id, _, turn, _ = answered_faq(conn, config)
    conn.execute(
        "UPDATE conversation_turns SET chat_type='group',thread_id='omt_tracked' WHERE turn_id=?",
        (turn["turn_id"],),
    )
    ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_peer_followup",
        payload={"content": "SYNTHETIC peer has a follow-up log", "chat_type": "group"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_other_colleague",
        chat_id="oc_chat",
        thread_id="omt_tracked",
    )
    before = list(conn.iterdump())
    snapshot = workbench_snapshot(conn, config=cfg)
    assert snapshot["counts"]["answered_faq"] == 0
    assert any(item["item_id"] == f"case:{case_id}" for item in snapshot["items"])
    assert list(conn.iterdump()) == before


def test_old_resolve_button_cannot_resolve_new_turn_with_same_numeric_fence(
    conn,
    config,
    tmp_path,
    monkeypatch,
):
    cfg, case_id, _, old_turn, _ = answered_faq(conn, config)
    configure_plugin(cfg, tmp_path, monkeypatch)
    old_preview = case_detail(conn, case_id=case_id)["preview"]
    callback = next(
        button["callback_data"]
        for button in old_preview["buttons"]
        if button["text"] == "标记解决"
    )
    old_version = conn.execute(
        "SELECT version FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()[0]
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_latest_followup",
        payload={"content": "SYNTHETIC latest unresolved input", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_colleague",
        chat_id="oc_chat",
    )
    new_turn = ensure_turn(conn, case_id=case_id, source_event_pk=event_pk)
    conn.execute(
        "UPDATE inbound_events SET status='processed' WHERE event_pk=?", (event_pk,)
    )
    conn.commit()
    assert old_turn["turn_id"] != new_turn["turn_id"]
    assert old_turn["fence"] == new_turn["fence"]
    assert (
        conn.execute(
            "SELECT version FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        == old_version
    )
    before = list(conn.iterdump())

    async def click_old_card():
        query = Query(callback)
        query.id = "independent-old-resolve"
        await plugin._handle_workbench_callback(query, callback)
        assert not query.edits, "a stale detail card changed the new conversation"
        assert "已有更新" in query.answers[-1]["text"]

    asyncio.run(click_old_card())
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("tamper", ["action", "cross_case", "token"])
def test_v2_action_token_rejects_substitution_through_real_local_cli(
    conn,
    config,
    tmp_path,
    monkeypatch,
    tamper,
):
    cfg, case_id, _, _, _ = answered_faq(conn, config)
    callback = _resolve_callback(conn, case_id)
    prefix, action, target, token = callback.split(":")
    assert prefix == "wka2" and len(token) == 22
    if tamper == "action":
        action = "a"
    elif tamper == "cross_case":
        _, target, _, _, _ = answered_faq(conn, config, index="other")
    else:
        token = ("A" if token[0] != "A" else "B") + token[1:]
    configure_plugin(cfg, tmp_path, monkeypatch)
    conn.commit()
    before = list(conn.iterdump())
    result = _click(f"{prefix}:{action}:{target}:{token}")
    assert not result.edits and "已有更新" in result.answers[-1]["text"]
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("field", ["payload_json", "sender_id", "chat_id", "thread_id"])
def test_v2_action_token_rejects_same_source_event_changed_context(
    conn,
    config,
    tmp_path,
    monkeypatch,
    field,
):
    cfg, case_id, event_pk, _, _ = answered_faq(conn, config)
    callback = _resolve_callback(conn, case_id)
    configure_plugin(cfg, tmp_path, monkeypatch)
    value = "SYNTHETIC_CHANGED_COORDINATE"
    if field == "payload_json":
        value = json.dumps(
            {"content": "SYNTHETIC corrected problem", "chat_type": "p2p"}
        )
    # This deliberately changes neither the event PK nor Case/Turn counters.
    conn.execute(
        f"UPDATE inbound_events SET {field}=? WHERE event_pk=?", (value, event_pk)
    )
    conn.commit()
    before = list(conn.iterdump())
    result = _click(callback)
    assert not result.edits and "已有更新" in result.answers[-1]["text"]
    assert list(conn.iterdump()) == before


def test_v2_fresh_resolve_then_duplicate_preserves_round_and_device_lock(
    conn,
    config,
    tmp_path,
    monkeypatch,
):
    cfg, case_id, _, _, _ = answered_faq(conn, config)
    callback = _resolve_callback(conn, case_id)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO locks(lock_key,owner,scope,case_id,acquired_at,heartbeat_at,expires_at) VALUES('board1','synthetic-session','board',?,?,?,?)",
        (case_id, now, now, now),
    )
    old_round = conn.execute(
        "SELECT lifecycle_round FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()[0]
    old_locks = [tuple(row) for row in conn.execute("SELECT * FROM locks")]
    configure_plugin(cfg, tmp_path, monkeypatch)
    conn.commit()
    accepted = _click(callback, identifier="fresh-action")
    assert accepted.edits and "已由你标记解决" in accepted.answers[-1]["text"]
    assert tuple(
        conn.execute(
            "SELECT state,owner,lifecycle_round FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
    ) == ("resolved", "operator", old_round)
    assert [tuple(row) for row in conn.execute("SELECT * FROM locks")] == old_locks
    assert not conn.execute(
        "SELECT 1 FROM conversation_turns WHERE case_id=? AND communication_owner='ai'",
        (case_id,),
    ).fetchone()
    before = list(conn.iterdump())
    for identifier in ("fresh-action", "duplicate-other-query-id"):
        duplicate = _click(callback, identifier=identifier)
        assert not duplicate.edits and "已有更新" in duplicate.answers[-1]["text"]
        assert list(conn.iterdump()) == before


def test_old_numeric_action_button_is_invalid_and_does_not_launch_cli(
    conn,
    config,
    tmp_path,
    monkeypatch,
):
    cfg, case_id, _, turn, _ = answered_faq(conn, config)
    configure_plugin(cfg, tmp_path, monkeypatch)
    case = conn.execute(
        "SELECT version,lifecycle_round FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    callback = (
        f"wka:r:{case_id}:{case['version']}:{case['lifecycle_round']}:{turn['fence']}"
    )

    def no_legacy_cli(*_args, **_kwargs):
        raise AssertionError("old numeric card should be rejected before the CLI")

    monkeypatch.setattr(plugin, "_run_control", no_legacy_cli)
    before = list(conn.iterdump())
    result = _click(callback)
    assert not result.edits and "旧按钮已失效" in result.answers[-1]["text"]
    assert list(conn.iterdump()) == before


def test_longest_allowed_case_id_has_complete_v2_actions_within_telegram_limit(
    conn,
    config,
    tmp_path,
    monkeypatch,
):
    longest = "K3-" + "x" * 28
    monkeypatch.setattr("k3_support.store._next_case_id", lambda _conn, _at: longest)
    cfg, case_id, _, _, _ = answered_faq(conn, config)
    conn.execute("UPDATE cases SET version=99999999 WHERE case_id=?", (case_id,))
    preview = case_detail(conn, case_id=case_id)["preview"]
    actions = [
        button
        for button in preview["buttons"]
        if button["callback_data"].startswith("wka2:")
    ]
    assert {button["text"] for button in actions} == {
        "我来回复",
        "只给我建议",
        "交给 AI",
        "标记解决",
    }
    assert all(len(button["callback_data"].encode()) <= 64 for button in actions)
    assert all(len(button["callback_data"].split(":")[-1]) == 22 for button in actions)
    configure_plugin(cfg, tmp_path, monkeypatch)
    conn.commit()
    result = _click(
        next(
            button["callback_data"]
            for button in actions
            if button["text"] == "标记解决"
        )
    )
    assert result.edits and "已由你标记解决" in result.answers[-1]["text"]
    assert (
        conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()[
            0
        ]
        == "resolved"
    )


@pytest.mark.parametrize(
    "user,chat", [("stranger", "owner-chat"), ("owner-user", "wrong-chat")]
)
def test_current_v2_token_is_not_operator_authorization(
    conn,
    config,
    tmp_path,
    monkeypatch,
    user,
    chat,
):
    cfg, case_id, _, _, _ = answered_faq(conn, config)
    callback = _resolve_callback(conn, case_id)
    configure_plugin(cfg, tmp_path, monkeypatch)
    before = list(conn.iterdump())
    result = _click(callback, user=user, chat=chat)
    assert not result.edits and "身份不匹配" in result.answers[-1]["text"]
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize(
    "label,mode", [("我来回复", "silent"), ("只给我建议", "suggest_only")]
)
def test_fresh_v2_communication_action_uses_exact_latest_turn_when_timestamps_tie(
    conn,
    config,
    tmp_path,
    monkeypatch,
    label,
    mode,
):
    cfg, case_id, _, old_turn, _ = answered_faq(conn, config)
    created_at = conn.execute(
        "SELECT created_at FROM conversation_turns WHERE turn_id=?",
        (old_turn["turn_id"],),
    ).fetchone()[0]
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_same_timestamp",
        payload={"content": "SYNTHETIC newer stored turn", "chat_type": "p2p"},
        occurred_at=created_at,
        sender_id="ou_colleague",
        chat_id="oc_chat",
    )
    newest = ensure_turn(conn, case_id=case_id, source_event_pk=event_pk)
    conn.execute(
        "UPDATE inbound_events SET status='processed' WHERE event_pk=?", (event_pk,)
    )
    old_snapshot = tuple(
        conn.execute(
            "SELECT * FROM conversation_turns WHERE turn_id=?", (old_turn["turn_id"],)
        ).fetchone()
    )
    callback = next(
        button["callback_data"]
        for button in case_detail(conn, case_id=case_id)["preview"]["buttons"]
        if button["text"] == label
    )
    configure_plugin(cfg, tmp_path, monkeypatch)
    conn.commit()
    result = _click(callback)
    assert result.edits
    selected = conn.execute(
        "SELECT communication_owner,communication_mode,state FROM conversation_turns WHERE turn_id=?",
        (newest["turn_id"],),
    ).fetchone()
    assert tuple(selected) == ("human", mode, "human_hold")
    assert (
        tuple(
            conn.execute(
                "SELECT * FROM conversation_turns WHERE turn_id=?",
                (old_turn["turn_id"],),
            ).fetchone()
        )
        == old_snapshot
    )
    assert (
        conn.execute(
            "SELECT matched_turn_id FROM operator_activities WHERE external_id='telegram:workbench:crossreview-click'"
        ).fetchone()[0]
        == newest["turn_id"]
    )
