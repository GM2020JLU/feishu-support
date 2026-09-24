from __future__ import annotations

import asyncio
import html
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import k3_support.hermes_plugin as plugin
from k3_support.approvals import request_approval
from k3_support.case_detail import case_detail
from k3_support.delivery import claim_outbox
from k3_support.delivery_attempts import record_outcome, start_attempt
from k3_support.knowledge import create_candidate
from k3_support.store import create_case, enqueue_outbox
from k3_support.workbench import workbench_snapshot


def make_case(conn, index, state="investigating"):
    case_id, _ = create_case(
        conn, title=f"case-{index}", case_type="bug", severity="P2", confidence=0.8
    )
    conn.execute("UPDATE cases SET state=? WHERE case_id=?", (state, case_id))
    return case_id


def test_full_queues_have_exact_counts_and_stable_pages(conn, config):
    states = [
        "intake",
        "triage",
        "answering",
        "investigating",
        "waiting_board",
        "board_testing",
        "waiting_push",
        "monitoring",
        "paused",
        "escalated",
        "error",
        "takeover",
        "resolved",
        "cancelled",
    ]
    case_ids = {}
    for state in states:
        case_ids[state] = [make_case(conn, f"{state}-{i}", state) for i in range(10)]
    snapshot = workbench_snapshot(conn, config=config, limit=7)
    assert snapshot["total_items"] == 120
    assert snapshot["counts"]["ai_processing"] == 50
    assert snapshot["counts"]["waiting"] == 40
    assert snapshot["counts"]["human_hold"] == 10
    found = []
    for page in range(1, snapshot["page_count"] + 1):
        value = workbench_snapshot(
            conn,
            config=config,
            limit=7,
            page=page,
            expected_digest=snapshot["snapshot_digest"],
        )
        assert value["total_items"] == 120
        found.extend(item["case_id"] for item in value["items"])
    assert len(found) == len(set(found)) == 120
    assert set(case_ids["takeover"]) <= set(found)
    assert set(case_ids["paused"]) <= set(found)
    assert not set(case_ids["resolved"] + case_ids["cancelled"]) & set(found)
    human = workbench_snapshot(conn, config=config, view="human", limit=50)
    assert {item["case_id"] for item in human["items"]} == set(case_ids["takeover"])


def test_each_non_case_queue_counts_past_one_hundred_without_raw_payloads(conn, config):
    case_id = make_case(conn, "approval")
    now = datetime.now(UTC)
    for index in range(105):
        request_approval(
            conn,
            approval_type="board1_lease",
            case_id=case_id,
            action={"case_id": case_id, "session": str(index)},
            expires_at=(now + timedelta(days=1)).isoformat(),
        )
        create_candidate(
            conn,
            title=f"FAQ {index}",
            questions=["help"],
            answer_markdown="guide",
            project="K3",
            module="EC",
            software_version=None,
            disclosure_class="internal",
            confidence=0.9,
            source_authority=0.9,
            canonical_case_id=None,
            source_digest=f"source-{index}",
        )
        conn.execute(
            """INSERT INTO outbox(outbox_id,channel,action_type,destination,payload_json,
                       idempotency_key,state,created_at,updated_at,global_outbound_fence)
               VALUES(?,'telegram','notify','owner',?,?,'permanent_failure',?,?,1)""",
            (
                f"failed-{index}",
                json.dumps({"private_body": "DO_NOT_LEAK_IN_LIST"}),
                f"failed-{index}",
                now.isoformat(),
                now.isoformat(),
            ),
        )
    for view, kind in (
        ("approvals", "approval"),
        ("knowledge", "knowledge_review"),
        ("errors", "delivery_failure"),
    ):
        result = workbench_snapshot(conn, config=config, view=view, limit=50)
        assert result["counts"][kind] == result["total_items"] == 105
        assert len(result["items"]) == 50 and result["page_count"] == 3
        assert "DO_NOT_LEAK_IN_LIST" not in json.dumps(result)


def test_changed_queue_cannot_retarget_an_old_item_or_page(conn, config):
    case_id = make_case(conn, "first")
    old = workbench_snapshot(conn, config=config)
    conn.execute("UPDATE cases SET title='new-title' WHERE case_id=?", (case_id,))
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="snapshot is stale"):
        workbench_snapshot(conn, config=config, expected_digest=old["snapshot_digest"])
    assert list(conn.iterdump()) == before


def test_case_detail_preserves_evidence_boundary_and_long_data(conn):
    case_id = make_case(conn, "<reported failure>")
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO case_sources(source_id,case_id,source_type,stable_external_id,
                   title,url,source_version,visibility,requester_access,metadata_json)
           VALUES('source-detail',?,'git','repo:commit','board1 build',
                  'https://example.test/evidence','commit-1','internal','allowed',?)""",
        (case_id, json.dumps({"board": "board1", "build": "canary"})),
    )
    conn.execute(
        """INSERT INTO evidence(evidence_id,case_id,source_id,evidence_layer,freshness_at,
                   visibility,claim,result,created_at)
           VALUES('evidence-detail',?,'source-detail','ram_boot',?,'internal',?,?,?)""",
        (case_id, now, "测试记录" * 2500, "本板未复现；对方环境尚未核对", now),
    )
    first = case_detail(conn, case_id=case_id)["preview"]
    before = list(conn.iterdump())
    bodies = []
    for page in range(1, first["page_count"] + 1):
        value = case_detail(
            conn, case_id=case_id, page=page, expected_digest=first["content_digest"]
        )["preview"]
        assert len(value["text"].encode("utf-16-le")) // 2 < 4096
        bodies.append(html.unescape(value["text"].split("\n\n", 1)[1]))
        assert all(
            len(button["callback_data"].encode()) <= 64 for button in value["buttons"]
        )
    text = "".join(bodies)
    assert "测试记录" * 2500 in text
    assert "本板未复现；对方环境尚未核对" in text
    assert "[ram_boot]" in text and "commit-1" in text and "board1" in text
    assert "暂无审查结论" in text and "不能证明对方故障已解决" in text
    assert "下一步（记录）：未记录" in text
    assert list(conn.iterdump()) == before
    conn.execute("UPDATE cases SET version=version+1 WHERE case_id=?", (case_id,))
    with pytest.raises(ValueError, match="detail is stale"):
        case_detail(
            conn, case_id=case_id, page=2, expected_digest=first["content_digest"]
        )


def test_case_detail_tracks_uncertain_send_until_actual_receipt(conn):
    case_id = make_case(conn, "takeover", "takeover")
    enqueue_outbox(
        conn,
        channel="feishu_im",
        action_type="reply",
        destination="om_original",
        payload={"text": "test", "identity": "user"},
        idempotency_key="detail-in-flight",
        case_id=case_id,
    )
    conn.commit()
    claimed = claim_outbox(conn, worker_id="fixture")
    assert claimed is not None
    assert "无法保证撤回" not in case_detail(conn, case_id=case_id)["preview"]["text"]
    assert start_attempt(conn, claimed)
    first = case_detail(conn, case_id=case_id)["preview"]
    assert "已有 1 次回复进入发送，结果尚未确认，无法保证撤回" in first["text"]
    assert claimed["claim_token"] not in first["text"]
    record_outcome(conn, claimed, event_type="uncertain", detail={"fixture": True})
    uncertain = case_detail(conn, case_id=case_id)["preview"]
    assert "无法保证撤回" in uncertain["text"]
    record_outcome(
        conn, claimed, event_type="delivered", detail={}, remote_id="om_sent"
    )
    assert "无法保证撤回" not in case_detail(conn, case_id=case_id)["preview"]["text"]
    with pytest.raises(ValueError, match="detail is stale"):
        case_detail(conn, case_id=case_id, expected_digest=uncertain["content_digest"])


class Query:
    from_user = SimpleNamespace(id="owner-user")
    message = SimpleNamespace(chat_id="owner-chat", message_id="menu-message")
    id = "workbench-callback"

    def __init__(self, data):
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)


def buttons(edit):
    markup = edit["reply_markup"]
    value = markup if isinstance(markup, dict) else markup.to_dict()
    return [button for row in value["inline_keyboard"] for button in row]


def test_real_telegram_menu_to_pages_and_detail_is_read_only_and_authenticated(
    conn,
    config,
    tmp_path,
    monkeypatch,
):
    for index in range(9):
        make_case(conn, index)
    takeover = make_case(conn, "manual", "takeover")
    knowledge = create_candidate(
        conn,
        title="<风扇指南>",
        questions=["如何调风扇"],
        answer_markdown="必须恢复温控。" * 700,
        project="K3",
        module="EC",
        software_version=None,
        disclosure_class="internal",
        confidence=0.9,
        source_authority=0.9,
        canonical_case_id=None,
        source_digest="preview-fixture",
    )
    conn.commit()
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    runtime = tmp_path / "workbench-plugin.json"
    runtime.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "control_cli": str(Path(sys.executable).parent / "k3-supportctl"),
                "control_config": str(config.path),
                "timeout_seconds": 10,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("K3_SUPPORT_CONTROL_PLUGIN_CONFIG", str(runtime))
    sent = []
    monkeypatch.setattr(
        plugin,
        "_send_button_message",
        lambda *args: sent.append(args) or "menu-message",
    )

    class Adapter:
        async def send(self, *args, **kwargs):
            raise AssertionError("unexpected fallback message")

        async def _handle_callback_query(self, update, context):
            raise AssertionError("workbench callback escaped into generic handler")

    def import_module(name):
        if name == "gateway.platform_registry":
            return SimpleNamespace(
                platform_registry=SimpleNamespace(get=lambda _name: None)
            )
        if name == "hermes_plugins.telegram_platform.adapter":
            return SimpleNamespace(TelegramAdapter=Adapter)
        raise ImportError(name)

    monkeypatch.setattr(plugin.importlib, "import_module", import_module)
    plugin._install_callback_handler()
    adapter = Adapter()

    async def click(data, *, user="owner-user", chat="owner-chat"):
        query = Query(data)
        query.from_user = SimpleNamespace(id=user)
        query.message = SimpleNamespace(chat_id=chat, message_id="menu-message")
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), None
        )
        return query

    async def exercise():
        event = SimpleNamespace(
            text="/feishu",
            message_id="workbench-menu-request",
            source=SimpleNamespace(
                platform="telegram", user_id="owner-user", chat_id="owner-chat"
            ),
        )
        await plugin._execute_and_reply(adapter, event)
        entry = next(
            button["callback_data"]
            for button in sent[0][2]
            if button["text"] == "待处理工作台"
        )
        before = list(conn.iterdump())
        first = await click(entry)
        assert first.edits, first.answers
        assert "支持工作台" in first.edits[0]["text"]
        page_button = next(
            button["callback_data"]
            for button in buttons(first.edits[0])
            if button["text"] == "下一页"
        )
        second = await click(page_button)
        assert "按首次登记排序，状态实时更新" in second.edits[0]["text"]
        item_button = next(
            button["callback_data"]
            for button in buttons(second.edits[0])
            if button["callback_data"].startswith("wi2:")
        )
        detail = await click(item_button)
        assert "Case 详情" in detail.edits[0]["text"]
        assert "暂无验证证据" in detail.edits[0]["text"]
        human_button = next(
            button["callback_data"]
            for button in buttons(first.edits[0])
            if button["text"] == "人工负责"
        )
        human = await click(human_button)
        assert takeover in html.unescape(human.edits[0]["text"])
        denied = await click(item_button, user="stranger")
        assert denied.edits == [] and "身份不匹配" in denied.answers[-1]["text"]
        wrong_chat = await click(item_button, chat="another-chat")
        assert wrong_chat.edits == [] and "身份不匹配" in wrong_chat.answers[-1]["text"]
        knowledge_button = next(
            button["callback_data"]
            for button in buttons(first.edits[0])
            if button["text"] == "知识"
        )
        knowledge_list = await click(knowledge_button)
        knowledge_item = next(
            button["callback_data"]
            for button in buttons(knowledge_list.edits[0])
            if button["callback_data"].startswith("wi2:")
        )
        preview = await click(knowledge_item)
        assert "&lt;风扇指南&gt;" in preview.edits[0]["text"]
        assert "待审核" in preview.edits[0]["text"]
        next_knowledge = next(
            button["callback_data"]
            for button in buttons(preview.edits[0])
            if button["text"] == "下一页"
        )
        knowledge_page = await click(next_knowledge)
        assert "必须恢复温控" in knowledge_page.edits[0]["text"]
        assert any(
            button["text"] == "返回工作台"
            for button in buttons(knowledge_page.edits[0])
        )
        denied_page = await click(next_knowledge, chat="other-chat")
        assert not denied_page.edits and "身份不匹配" in denied_page.answers[-1]["text"]
        assert list(conn.iterdump()) == before
        make_case(conn, "new-item")
        conn.commit()
        still_valid = await click(item_button)
        assert still_valid.edits and "Case 详情" in still_valid.edits[0]["text"]
        malformed = await click("wki:all:1:1:approve")
        assert malformed.edits == []

    asyncio.run(exercise())
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT status FROM knowledge_entries WHERE knowledge_id=?",
            (knowledge,),
        ).fetchone()[0]
        == "candidate"
    )
