from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import yaml

import k3_support.hermes_plugin as plugin
from k3_support.approvals import expiry_after, normalized_board_action, request_approval
from k3_support.hermes_plugin import pre_gateway_dispatch, register
from k3_support.store import create_case


class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, reply_to, metadata))
        return SimpleNamespace(success=True, message_id="receipt-1")


def test_remote_recovery_callback_uses_deterministic_control_and_original_prompt(monkeypatch):
    captured, displayed = [], []
    def control(event):
        captured.append(event)
        return True, {"command": "remote_recovery", "preview": {"text": "确认？", "buttons": []}}
    async def show(query, ok, value):
        displayed.append((query, ok, value))
    monkeypatch.setattr(plugin, "_run_control", control)
    monkeypatch.setattr(plugin, "_show_workbench_preview", show)
    query = SimpleNamespace(from_user=SimpleNamespace(id="owner"),
                            message=SimpleNamespace(chat_id="chat", message_id="42"), id="callback-1")
    data = "rr:q:" + "a" * 32
    asyncio.run(plugin._handle_meeting_callback(query, data))
    assert captured[0].text == "remote-recovery 42 " + data
    assert captured[0].source.user_id == "owner" and captured[0].source.chat_id == "chat"
    assert displayed[0][0] is query and displayed[0][1]
    assert plugin._inline_keyboard([{"text": "核对", "callback_data": data}])[0][0]["callback_data"] == data


async def wait_for_receipts(adapter: FakeAdapter, count: int = 1) -> None:
    for _ in range(200):
        if len(adapter.sent) >= count:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {count} receipt(s)")


def event(text, *, user="owner-user", chat="owner-chat", message="tg-1"):
    source = SimpleNamespace(
        platform="telegram", user_id=user, chat_id=chat, message_id=message
    )
    return SimpleNamespace(
        text=text, source=source, message_id=message, raw_message=None
    )


def gateway(adapter):
    return SimpleNamespace(adapters={"telegram": adapter})


def write_runtime(tmp_path: Path, config_path: Path) -> Path:
    runtime = tmp_path / "control-plugin.json"
    runtime.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "control_cli": str(
                    (Path(__file__).parents[1] / ".venv/bin/k3-supportctl").resolve()
                ),
                "control_config": str(config_path.resolve()),
                "timeout_seconds": 10,
            }
        ),
        encoding="utf-8",
    )
    return runtime


def test_non_control_text_falls_through_without_reply():
    adapter = FakeAdapter()
    assert pre_gateway_dispatch(event("请帮我看看 board1"), gateway(adapter)) is None
    assert pre_gateway_dispatch(event("/k3"), gateway(adapter)) is None
    assert adapter.sent == []


def test_feishu_command_sends_and_binds_global_panel(monkeypatch):
    monkeypatch.setattr(
        plugin,
        "_run_control",
        lambda _event: (
            True,
            {
                "command": "global_panel",
                "panel_id": "gcp_panel1",
                "text": "飞书自动办公总控",
                "buttons": [
                    {"text": "观察", "callback_data": "fsc:o:gcp_panel1", "row": 0}
                ],
            },
        ),
    )
    sent = []
    bound = []
    monkeypatch.setattr(
        plugin,
        "_send_button_message",
        lambda chat, text, buttons: sent.append((chat, text, buttons)) or "panel-msg",
    )
    monkeypatch.setattr(
        plugin,
        "_run_panel_bind",
        lambda **kwargs: bound.append(kwargs) or (True, {"bound": True}),
    )

    async def exercise():
        adapter = FakeAdapter()
        outcome = pre_gateway_dispatch(event("/feishu"), gateway(adapter))
        assert outcome == {"action": "skip", "reason": "k3-support-control"}
        for _ in range(200):
            if bound:
                break
            await asyncio.sleep(0.005)
        assert adapter.sent == []

    asyncio.run(exercise())
    assert sent[0][0:2] == ("owner-chat", "飞书自动办公总控")
    assert bound == [
        {
            "user_id": "owner-user",
            "chat_id": "owner-chat",
            "command_message_id": "tg-1",
            "prompt_message_id": "panel-msg",
            "panel_id": "gcp_panel1",
        }
    ]


def test_global_callback_maps_compact_action_and_refreshes_same_message(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        plugin,
        "_run_callback",
        lambda **kwargs: captured.update(kwargs)
        or (
            True,
                {
                    "command": "global_panel",
                    "mode": "collaborate",
                    "revision": 2,
                    "text": "当前模式：协作",
                "buttons": [
                    {"text": "观察", "callback_data": "fsc:o:gcp_panel1", "row": 0}
                ],
            },
        ),
    )

    class Query:
        id = "callback-global"
        from_user = SimpleNamespace(id="owner-user")
        message = SimpleNamespace(chat_id="owner-chat", message_id="panel-msg")

        def __init__(self):
            self.answers = []
            self.edits = []

        async def answer(self, **kwargs):
            self.answers.append(kwargs)

        async def edit_message_text(self, **kwargs):
            self.edits.append(kwargs)

    query = Query()
    asyncio.run(plugin._handle_global_callback(query, "fsc:c:gcp_panel1"))
    assert captured == {
        "user_id": "owner-user",
        "chat_id": "owner-chat",
        "prompt_message_id": "panel-msg",
        "callback_query_id": "callback-global",
        "action": "global_collaborate",
        "target_id": "gcp_panel1",
        "target_type": "panel",
    }
    assert query.answers == [{"text": "已切换：协作", "show_alert": True}]
    assert query.edits[0]["text"] == "✅ 已切换：协作\n当前模式：协作"
    assert query.edits[0]["reply_markup"] is None


def test_control_plugin_executes_real_cli_and_replies_idempotently(
    conn, config, tmp_path, monkeypatch
):
    config.path.write_text(
        yaml.safe_dump(config.raw, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    case_id, _ = create_case(
        conn, title="plugin canary", case_type="bug", severity="P2", confidence=0.5
    )
    action = normalized_board_action(case_id, "session-plugin", 15)
    approval_id, action_digest, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="session-plugin",
        action=action,
        expires_at=expiry_after(30),
    )
    runtime = write_runtime(tmp_path, config.path)
    monkeypatch.setenv("K3_SUPPORT_CONTROL_PLUGIN_CONFIG", str(runtime))
    command = f"approve board {approval_id} {action_digest}"

    async def exercise():
        adapter = FakeAdapter()
        outcome = pre_gateway_dispatch(event(command), gateway(adapter))
        assert outcome == {"action": "skip", "reason": "k3-support-control"}
        await wait_for_receipts(adapter)
        assert len(adapter.sent) == 1
        assert "已批准 board" in adapter.sent[0][1]
        assert approval_id in adapter.sent[0][1]

        # A lost receipt can be retried as a new Telegram message. The durable
        # decision remains single-shot and the operator receives the same state.
        replay = event(command, message="tg-2")
        outcome = pre_gateway_dispatch(replay, gateway(adapter))
        assert outcome == {"action": "skip", "reason": "k3-support-control"}
        await wait_for_receipts(adapter, 2)
        assert len(adapter.sent) == 2
        assert "已批准 board" in adapter.sent[1][1]

    asyncio.run(exercise())
    row = conn.execute(
        "SELECT status,approval_message_id,approver_identity FROM approvals WHERE approval_id=?",
        (approval_id,),
    ).fetchone()
    assert tuple(row) == ("approved", "tg-1", "owner-user")


def test_control_plugin_fails_closed_for_forged_identity(
    conn, config, tmp_path, monkeypatch
):
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    case_id, _ = create_case(
        conn, title="forged", case_type="bug", severity="P2", confidence=0.5
    )
    action = normalized_board_action(case_id, "session-forged", 15)
    approval_id, action_digest, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="session-forged",
        action=action,
        expires_at=expiry_after(30),
    )
    runtime = write_runtime(tmp_path, config.path)
    monkeypatch.setenv("K3_SUPPORT_CONTROL_PLUGIN_CONFIG", str(runtime))

    async def exercise():
        adapter = FakeAdapter()
        command = f"approve board {approval_id} {action_digest}"
        outcome = pre_gateway_dispatch(
            event(command, user="forged-user"), gateway(adapter)
        )
        assert outcome == {"action": "skip", "reason": "k3-support-control"}
        await wait_for_receipts(adapter)
        assert adapter.sent[0][1] == "❌ 控制命令未执行：控制身份不匹配"
        assert "Traceback" not in adapter.sent[0][1]

    asyncio.run(exercise())
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()[0]
        == "requested"
    )


def test_malformed_control_is_consumed_without_llm_dispatch(tmp_path, monkeypatch):
    runtime = tmp_path / "missing-runtime.json"
    monkeypatch.setenv("K3_SUPPORT_CONTROL_PLUGIN_CONFIG", str(runtime))

    async def exercise():
        adapter = FakeAdapter()
        outcome = pre_gateway_dispatch(event("approve board nonsense"), gateway(adapter))
        assert outcome == {"action": "skip", "reason": "k3-support-control"}
        await wait_for_receipts(adapter)
        assert "尚未配置" in adapter.sent[0][1]

    asyncio.run(exercise())


def test_control_missing_stable_message_id_fails_closed():
    async def exercise():
        adapter = FakeAdapter()
        value = event("status K3-20260901-0001", message="")
        outcome = pre_gateway_dispatch(value, gateway(adapter))
        assert outcome == {"action": "skip", "reason": "k3-support-control"}
        await wait_for_receipts(adapter)
        assert "缺少稳定的 Telegram" in adapter.sent[0][1]

    asyncio.run(exercise())


def test_control_timeout_and_invalid_output_fail_closed(tmp_path, monkeypatch):
    cli = executable = tmp_path / "k3-supportctl"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    control = tmp_path / "support.yaml"
    control.write_text("mode: shadow\n", encoding="utf-8")
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "control_cli": str(cli.resolve()),
                "control_config": str(control.resolve()),
                "timeout_seconds": 1,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("K3_SUPPORT_CONTROL_PLUGIN_CONFIG", str(runtime))

    async def receipt_with(fake_run):
        monkeypatch.setattr("k3_support.hermes_plugin.subprocess.run", fake_run)
        adapter = FakeAdapter()
        outcome = pre_gateway_dispatch(
            event("status K3-20260901-0001"), gateway(adapter)
        )
        assert outcome == {"action": "skip", "reason": "k3-support-control"}
        await wait_for_receipts(adapter)
        return adapter.sent[0][1]

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=1)

    assert "处理超时" in asyncio.run(receipt_with(timeout))

    def invalid(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="not-json", stderr="")

    assert "无效回执" in asyncio.run(receipt_with(invalid))


def test_adapter_exception_still_consumes_control_message(monkeypatch):
    def broken(_event):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("k3_support.hermes_plugin._run_control", broken)

    async def exercise():
        adapter = FakeAdapter()
        outcome = pre_gateway_dispatch(
            event("status K3-20260901-0001"), gateway(adapter)
        )
        assert outcome == {"action": "skip", "reason": "k3-support-control"}
        await wait_for_receipts(adapter)
        assert "适配器内部错误" in adapter.sent[0][1]

    asyncio.run(exercise())


def test_control_subprocess_does_not_block_gateway_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def delayed(_event):
        started.set()
        assert release.wait(timeout=2)
        return True, {"command": "status", "case": {"case_id": "K3-1"}}

    monkeypatch.setattr("k3_support.hermes_plugin._run_control", delayed)

    async def exercise():
        adapter = FakeAdapter()
        before = time.monotonic()
        outcome = pre_gateway_dispatch(
            event("status K3-20260901-0001"), gateway(adapter)
        )
        elapsed = time.monotonic() - before
        assert outcome == {"action": "skip", "reason": "k3-support-control"}
        assert elapsed < 0.05
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        assert adapter.sent == []
        release.set()
        await wait_for_receipts(adapter)

    asyncio.run(exercise())


def test_expired_approval_is_rejected_by_real_cli(conn, config, tmp_path, monkeypatch):
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    case_id, _ = create_case(
        conn, title="expired", case_type="bug", severity="P2", confidence=0.5
    )
    action = normalized_board_action(case_id, "session-expired", 15)
    approval_id, action_digest, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="session-expired",
        action=action,
        expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    runtime = write_runtime(tmp_path, config.path)
    monkeypatch.setenv("K3_SUPPORT_CONTROL_PLUGIN_CONFIG", str(runtime))

    async def exercise():
        adapter = FakeAdapter()
        command = f"approve board {approval_id} {action_digest}"
        pre_gateway_dispatch(event(command), gateway(adapter))
        await wait_for_receipts(adapter)
        assert adapter.sent[0][1] == "❌ 控制命令未执行：审批请求已过期"

    asyncio.run(exercise())
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()[0]
        == "expired"
    )


def test_register_remains_compatible_with_hook_only_context():
    calls = []
    register(SimpleNamespace(register_hook=lambda *args: calls.append(args)))
    assert calls == [("pre_gateway_dispatch", pre_gateway_dispatch)]


def test_callback_handler_wraps_live_hermes_plugin_namespace(monkeypatch):
    calls = []

    class LiveTelegramAdapter:
        async def _handle_callback_query(self, update, context):
            calls.append(("original", update, context))

    live_module = SimpleNamespace(TelegramAdapter=LiveTelegramAdapter)
    registry = SimpleNamespace(
        platform_registry=SimpleNamespace(
            get=lambda name: calls.append(("resolve", name))
        )
    )

    def import_module(name):
        calls.append(("import", name))
        if name == "gateway.platform_registry":
            return registry
        if name == "hermes_plugins.telegram_platform.adapter":
            return live_module
        raise AssertionError(f"unexpected compatibility import: {name}")

    handled = []

    async def handle_global(query, data):
        handled.append((query, data))

    monkeypatch.setattr(plugin.importlib, "import_module", import_module)
    monkeypatch.setattr(plugin, "_handle_global_callback", handle_global)
    plugin._install_callback_handler()

    query = SimpleNamespace(data="fsc:r:gcp_panel1")
    update = SimpleNamespace(callback_query=query)
    asyncio.run(LiveTelegramAdapter()._handle_callback_query(update, object()))

    assert calls == [
        ("import", "gateway.platform_registry"),
        ("resolve", "telegram"),
        ("import", "hermes_plugins.telegram_platform.adapter"),
    ]
    assert handled == [(query, "fsc:r:gcp_panel1")]


def test_callback_handler_falls_back_to_legacy_telegram_namespace(monkeypatch):
    imports = []

    class LegacyTelegramAdapter:
        async def _handle_callback_query(self, update, context):
            return None

    def import_module(name):
        imports.append(name)
        if name == "gateway.platform_registry":
            return SimpleNamespace(
                platform_registry=SimpleNamespace(get=lambda _name: None)
            )
        if name == "hermes_plugins.telegram_platform.adapter":
            raise ImportError(name)
        if name == "plugins.platforms.telegram.adapter":
            return SimpleNamespace(TelegramAdapter=LegacyTelegramAdapter)
        raise AssertionError(name)

    monkeypatch.setattr(plugin.importlib, "import_module", import_module)
    plugin._install_callback_handler()

    assert imports == [
        "gateway.platform_registry",
        "hermes_plugins.telegram_platform.adapter",
        "plugins.platforms.telegram.adapter",
    ]
    assert getattr(
        LegacyTelegramAdapter._handle_callback_query, "_k3_support_wrapped", False
    )


def test_register_exposes_feishu_as_native_slash_command(monkeypatch):
    hooks = []
    slash_commands = []
    monkeypatch.setattr(plugin, "_install_callback_handler", lambda: None)
    register(
        SimpleNamespace(
            register_hook=lambda *args: hooks.append(args),
            register_command=lambda *args, **kwargs: slash_commands.append(
                (args, kwargs)
            ),
        )
    )
    assert hooks == [("pre_gateway_dispatch", pre_gateway_dispatch)]
    assert slash_commands == [
        (
            ("feishu",),
            {
                "handler": plugin._feishu_slash_fallback,
                "description": "打开飞书自动办公总控",
            },
        )
    ]
    assert plugin._feishu_slash_fallback("") == (
        "请在已配置的 Telegram 私聊中发送 /feishu 打开飞书自动办公总控。"
    )
    assert plugin._feishu_slash_fallback("unexpected") == "用法：/feishu"


def test_register_exposes_button_sender_and_sender_builds_inline_keyboard(
    monkeypatch, capsys
):
    hooks = []
    commands = []
    slash_commands = []
    monkeypatch.setattr(plugin, "_install_callback_handler", lambda: None)
    register(
        SimpleNamespace(
            register_hook=lambda *args: hooks.append(args),
            register_command=lambda *args, **kwargs: slash_commands.append(
                (args, kwargs)
            ),
            register_cli_command=lambda *args: commands.append(args),
        )
    )
    assert hooks == [("pre_gateway_dispatch", pre_gateway_dispatch)]
    assert slash_commands[0][0] == ("feishu",)
    assert commands[0][0] == "k3-support-telegram"

    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {"ok": True, "result": {"message_id": 77}}
            ).encode()

    class Opener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode()
            captured["timeout"] = timeout
            return Response()

    monkeypatch.setattr(plugin, "_telegram_token", lambda: "test-token")
    monkeypatch.setattr(plugin.urllib.request, "build_opener", lambda *_: Opener())
    plugin._telegram_cli(
        SimpleNamespace(
            k3_support_telegram_command="send-buttons",
            to="telegram:123",
            text="审批？",
            buttons_json=json.dumps(
                [{"text": "同意", "callback_data": "k3a:a:apr_123"}]
            ),
            parse_mode="HTML",
        )
    )
    output = json.loads(capsys.readouterr().out)
    assert output == {"ok": True, "message_id": 77}
    assert "test-token" in captured["url"]
    assert "inline_keyboard" in captured["body"]
    assert "parse_mode=HTML" in captured["body"]
    assert captured["timeout"] == 20


def test_plugin_has_no_nonstdlib_imports():
    source = Path(__file__).parents[1] / "src/k3_support/hermes_plugin/__init__.py"
    text = source.read_text(encoding="utf-8")
    assert "from k3_support" not in text
    assert "import yaml" not in text
    assert "shell=True" not in text
    assert os.path.isabs(str(source))


def test_case_callback_uses_compact_button_and_exact_prompt(monkeypatch):
    captured = {}

    def run_callback(**kwargs):
        captured.update(kwargs)
        return True, {
            "command": "claim",
            "case_id": "K3-20260902-0001",
            "turn": {"communication_owner": "human", "communication_mode": "silent"},
            "in_flight_deliveries": [{"claim_token": "private-attempt-token", "state": "in_flight"}],
        }

    monkeypatch.setattr(plugin, "_run_callback", run_callback)

    class Query:
        id = "callback-1"
        from_user = SimpleNamespace(id="owner-user")
        message = SimpleNamespace(chat_id="owner-chat", message_id="prompt-1")

        def __init__(self):
            self.answers = []
            self.edits = []

        async def answer(self, **kwargs):
            self.answers.append(kwargs)

        async def edit_message_text(self, **kwargs):
            self.edits.append(kwargs)

    query = Query()
    asyncio.run(plugin._handle_k3_case_callback(query, "k3c:c:K3-20260902-0001"))
    assert captured == {
        "user_id": "owner-user",
        "chat_id": "owner-chat",
        "prompt_message_id": "prompt-1",
        "callback_query_id": "callback-1",
        "action": "claim",
        "target_id": "K3-20260902-0001",
        "target_type": "case",
    }
    assert query.answers == [{"text": "已处理", "show_alert": False}]
    assert "我来回复" in query.edits[0]["text"]
    assert "已有 1 次回复进入发送，结果尚未确认，无法保证撤回" in query.edits[0]["text"]
    assert "private-attempt-token" not in query.edits[0]["text"]
