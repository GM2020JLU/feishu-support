from __future__ import annotations

import asyncio
import html
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import k3_support.hermes_plugin as plugin
from k3_support.approvals import ApprovalError
from k3_support.control import ControlError, ControlMessage, execute_control
from k3_support.knowledge import create_candidate


def candidate(conn, *, answer="先确认板型，再参考文档。"):
    knowledge_id = create_candidate(
        conn,
        title="Pico <风扇> & EC",
        questions=["怎么调风扇"],
        answer_markdown=answer,
        project="K3",
        module="EC",
        software_version="v1",
        disclosure_class="internal",
        confidence=0.9,
        source_authority=0.9,
        canonical_case_id=None,
        source_digest="preview-test-source",
    )
    conn.execute(
        """INSERT INTO knowledge_sources(mapping_id,knowledge_id,source_type,
                   stable_external_id,url,source_version,visibility,claim)
           VALUES('mapping-preview',?,'feishu_doc','doc-preview',
                  'https://example.test/doc?a=1&b=2','42','internal','Only Pico')""",
        (knowledge_id,),
    )
    return knowledge_id


def show(conn, config, knowledge_id, *, page=None, fingerprint=None, user="owner-user"):
    text = f"knowledge show {knowledge_id}"
    if page is not None:
        text += f" {page} {fingerprint}"
    return execute_control(
        conn, config, ControlMessage(user, "owner-chat", "show-preview", text)
    )


def test_show_is_read_only_and_renders_auditable_legacy_content(conn, config):
    knowledge_id = candidate(
        conn, answer="<script>do not run</script>\n查看 `pwm1` & 保留原值"
    )
    before = list(conn.iterdump())
    result = show(conn, config, knowledge_id)
    preview = result["preview"]
    text = preview["text"]
    assert result["operation"] == "show"
    assert "Pico &lt;风扇&gt; &amp; EC" in text
    assert "&lt;script&gt;do not run&lt;/script&gt;" in text
    assert "状态：待审核" in text
    assert "版本：42" in text
    assert "https://example.test/doc?a=1&amp;b=2" in text
    assert "软件版本：v1" in text
    assert "警告：未记录" in text
    assert "支持的结论：Only Pico" in text
    assert "已处理" not in text
    assert "只读查看" in text
    assert plugin._format_receipt(True, result) == text
    assert list(conn.iterdump()) == before


def test_every_character_of_long_answer_and_constraints_is_accessible(conn, config):
    answer = "起点" + ("<a>&😀调节风扇\n" * 850) + "终点：必须恢复温控"
    knowledge_id = candidate(conn, answer=answer)
    first = show(conn, config, knowledge_id)["preview"]
    assert first["page_count"] > 3
    fragments = []
    for number in range(1, first["page_count"] + 1):
        preview = show(
            conn, config, knowledge_id, page=number, fingerprint=first["content_digest"]
        )["preview"]
        assert len(preview["text"].encode("utf-16-le")) // 2 < 4096
        fragments.append(html.unescape(preview["text"].split("\n\n", 1)[1]))
        for button in preview["buttons"]:
            assert len(button["callback_data"].encode()) <= 64
            assert button["callback_data"].startswith("knp:")
            assert "approve" not in button["callback_data"]
    full = "".join(fragments)
    assert answer in full
    assert "前提与警告" in full and "来源与定位" in full
    assert "https://example.test/doc?a=1&b=2" in full


@pytest.mark.parametrize("change", ["answer", "source", "review"])
def test_changed_content_or_source_or_review_rejects_old_page(conn, config, change):
    knowledge_id = candidate(conn, answer="长答案" * 2000)
    fingerprint = show(conn, config, knowledge_id)["preview"]["content_digest"]
    if change == "answer":
        conn.execute(
            "UPDATE knowledge_entries SET answer_markdown='新答案' WHERE knowledge_id=?",
            (knowledge_id,),
        )
    elif change == "source":
        conn.execute(
            "UPDATE knowledge_sources SET source_version='43' WHERE knowledge_id=?",
            (knowledge_id,),
        )
    else:
        conn.execute(
            "UPDATE knowledge_entries SET status='stale' WHERE knowledge_id=?",
            (knowledge_id,),
        )
    before = list(conn.iterdump())
    with pytest.raises(ControlError, match="preview is stale"):
        show(conn, config, knowledge_id, page=2, fingerprint=fingerprint)
    assert list(conn.iterdump()) == before


def test_show_requires_control_identity_and_valid_page(conn, config):
    knowledge_id = candidate(conn)
    with pytest.raises(ApprovalError, match="control identity mismatch"):
        show(conn, config, knowledge_id, user="stranger")
    fingerprint = show(conn, config, knowledge_id)["preview"]["content_digest"]
    with pytest.raises(ControlError, match="out of range"):
        show(conn, config, knowledge_id, page=0, fingerprint=fingerprint)


def test_professional_preview_includes_scope_warnings_and_current_lifecycle(
    conn, config
):
    knowledge_id = candidate(conn)
    metadata = {
        "scope": {
            "product": "K3",
            "boards": ["pico-itx"],
            "software_versions": ["EC-v2"],
        },
        "intent": {
            "required_entities": ["board"],
            "negative_constraints": ["不适用其他板型"],
        },
        "content": {
            "warnings": ["勿关闭温控 <danger>"],
            "prerequisites": ["温度正常"],
            "failure_branches": ["故障时转人工"],
            "rollback": ["归还控制权"],
        },
        "sources": [
            {
                "title": "EC 固件指南",
                "url": "https://example.test/ec",
                "version": "7",
                "locator": {"revision": "7", "block": "fan"},
                "share_mode": "link_only",
            }
        ],
    }
    conn.execute(
        """INSERT INTO professional_knowledge_revisions(revision_id,stable_id,revision_number,
                   knowledge_id,kind,lifecycle_state,revision_digest,payload_json,body_markdown,
                   owner,reviewed_by,reviewed_at,review_due_at,imported_at)
           VALUES('pkr-preview','k3.preview',1,?,'document_route','needs_review',
                  'revision-digest',?,'body','owner','owner','2026-09-01',
                  '2026-10-01','2026-09-01')""",
        (knowledge_id, json.dumps(metadata)),
    )
    conn.execute(
        "UPDATE knowledge_entries SET professional_revision_id='pkr-preview' WHERE knowledge_id=?",
        (knowledge_id,),
    )
    text = show(conn, config, knowledge_id)["preview"]["text"]
    assert "专业知识状态：需要复审" in text
    assert "pico-itx" in text and "EC-v2" in text
    assert "不适用其他板型" in text
    assert "勿关闭温控 &lt;danger&gt;" in text
    assert "故障时转人工" in text and "归还控制权" in text
    assert "link_only" in text and "fan" in text


class Query:
    from_user = SimpleNamespace(id="owner-user")
    message = SimpleNamespace(chat_id="owner-chat", message_id="preview-message")
    id = "preview-callback"

    def __init__(self):
        self.answers = []
        self.edits = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)


def test_plugin_real_cli_show_and_pages_are_authenticated_and_stale_safe(
    conn,
    config,
    tmp_path,
    monkeypatch,
):
    knowledge_id = candidate(conn, answer="完整测试答案😀" * 1800)
    conn.commit()
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    runtime = tmp_path / "knowledge-plugin.json"
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
        lambda *args: sent.append(args) or "preview-message",
    )
    event = SimpleNamespace(
        text=f"knowledge show {knowledge_id}",
        message_id="preview-request",
        source=SimpleNamespace(
            platform="telegram", user_id="owner-user", chat_id="owner-chat"
        ),
    )

    class Adapter:
        async def send(self, *args, **kwargs):
            raise AssertionError(
                "show must render an HTML preview, not a generic receipt"
            )

        async def _handle_callback_query(self, update, context):
            raise AssertionError(
                "knowledge callbacks must not reach Hermes' generic handler"
            )

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

    async def exercise():
        assert (
            plugin.pre_gateway_dispatch(
                event, SimpleNamespace(adapters={"telegram": Adapter()})
            )["action"]
            == "skip"
        )
        for _ in range(300):
            if sent:
                break
            await asyncio.sleep(0.01)
        assert sent and sent[0][3] == "HTML"
        data = sent[0][2][0]["callback_data"]
        query = Query()
        query.data = data
        await Adapter()._handle_callback_query(
            SimpleNamespace(callback_query=query), None
        )
        assert query.edits and query.edits[0]["parse_mode"] == "HTML"
        assert "知识预览 · 2/" in query.edits[0]["text"]
        stranger = Query()
        stranger.from_user = SimpleNamespace(id="stranger")
        await plugin._handle_knowledge_callback(stranger, data)
        assert stranger.edits == []
        assert "身份不匹配" in stranger.answers[-1]["text"]
        conn.execute(
            "UPDATE knowledge_entries SET status='stale' WHERE knowledge_id=?",
            (knowledge_id,),
        )
        conn.commit()
        old = Query()
        await plugin._handle_knowledge_callback(old, data)
        assert old.edits == []
        assert "已更新" in old.answers[-1]["text"]
        malformed = Query()
        await plugin._handle_knowledge_callback(
            malformed, f"knp:{knowledge_id}:approve:2"
        )
        assert malformed.edits == []

    asyncio.run(exercise())
    assert (
        conn.execute(
            "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
        ).fetchone()[0]
        == "stale"
    )
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM global_control_state").fetchone()[0] == 0
def test_source_navigation_filters_unsafe_urls_and_deduplicates():
    from k3_support.knowledge_preview import _source_links

    allowed = {'title': '<b>Fan guide</b>', 'url': 'https://example.com/wiki/fan'}
    invalid = ['javascript:alert(1)', 'http://example.com', '//example.com',
               'https://user:secret@example.com', 'https://example.com:bad',
               'https://example.com\\evil', 'https://example.com/\nsecret',
               'https://', None, {'url': 'https://example.com'}]
    assert _source_links([allowed, allowed, *({'url': url} for url in invalid)]) == [allowed]
