import asyncio
import json
from types import SimpleNamespace

import pytest
import yaml
from test_feishu_control import setup_identity
from test_hermes_control_plugin import FakeAdapter, write_runtime

from k3_support.approvals import ApprovalError
from k3_support.control import ControlError, ControlMessage, execute_control
from k3_support.hermes_plugin import pre_gateway_dispatch
from k3_support.hermes_plugin.feishu_cards import NAMESPACE, callback_event, render
from k3_support.runtime_control import RuntimeControlError


def test_callback_requires_native_context_and_preserves_reply_target():
    source = SimpleNamespace(
        platform="feishu", user_id="tenant-user", chat_id="oc_control"
    )
    event = SimpleNamespace(text="/card button " + NAMESPACE, source=source)
    assert callback_event(event) == (True, None)
    event.raw_message = SimpleNamespace(
        event=SimpleNamespace(
            token="click-token",
            context=SimpleNamespace(
                open_chat_id="oc_control", open_message_id="om_card"
            ),
            operator=SimpleNamespace(open_id="ou_operator", user_id="tenant-user"),
            action=SimpleNamespace(value={NAMESPACE: "card-action fcc_example 0"}),
        )
    )
    owned, normalized = callback_event(event)
    assert owned and normalized.text == "card-action fcc_example 0"
    assert (
        normalized.message_id == "click-token"
        and normalized.control_reply_to == "om_card"
    )
    event.raw_message.event.context.open_chat_id = "oc_other"
    assert callback_event(event) == (True, None)
    assert callback_event(SimpleNamespace(text="/card button unrelated")) == (
        False,
        None,
    )


def test_approval_card_preserves_literal_complete_action():
    text = "<at id=all>**literal**</at>"
    card = render(
        {
            "command": "approval_detail",
            "card_id": "fcc_example",
            "text": text,
            "commands": ["approve id digest binding", "deny id digest binding"],
        }
    )
    assert card["body"]["elements"][0]["text"] == {"tag": "plain_text", "content": text}
    assert (
        render(
            {
                "command": "approval_detail",
                "card_id": "fcc_example",
                "text": "x" * 12001,
                "commands": [],
            }
        )
        is None
    )


def test_card_click_runs_cli_and_updates_mode(conn, config, tmp_path, monkeypatch):
    setup_identity(config)
    config.path.write_text(yaml.safe_dump(config.raw))
    runtime = write_runtime(tmp_path, config.path)
    monkeypatch.setenv("K3_SUPPORT_CONTROL_PLUGIN_CONFIG", str(runtime))

    async def exercise():
        class Adapter(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.cards = []

            async def _feishu_send_with_retry(self, **kwargs):
                self.cards.append(kwargs)
                return SimpleNamespace(success=True, message_id="om_card")

            def _finalize_send_result(self, response, _):
                return response

        adapter = Adapter()
        gateway = SimpleNamespace(adapters={"feishu": adapter})
        source = SimpleNamespace(
            platform="feishu", user_id="tenant-user", chat_id="oc_control"
        )
        event = SimpleNamespace(text="/feishu", message_id="om_open", source=source)
        assert pre_gateway_dispatch(event, gateway)["action"] == "skip"
        for _ in range(300):
            if (
                adapter.cards
                and conn.execute(
                    "SELECT count(*) FROM feishu_control_cards WHERE delivered_message_id IS NOT NULL"
                ).fetchone()[0]
            ):
                break
            await asyncio.sleep(0.01)
        assert len(adapter.cards) == 1
        card = json.loads(adapter.cards[0]["payload"])
        buttons = [row for row in card["body"]["elements"] if row["tag"] == "button"]
        value = next(
            row["behaviors"][0]["value"]
            for row in buttons
            if "暂停" in row["text"]["content"]
        )
        click = SimpleNamespace(
            text="/card button " + json.dumps(value),
            source=SimpleNamespace(
                platform="feishu", user_id="ou_operator", chat_id="oc_control"
            ),
            raw_message=SimpleNamespace(
                event=SimpleNamespace(
                    token="click-1",
                    context=SimpleNamespace(
                        open_chat_id="oc_control", open_message_id="om_card"
                    ),
                    operator=SimpleNamespace(
                        open_id="ou_operator", user_id="tenant-user"
                    ),
                    action=SimpleNamespace(value=value),
                )
            ),
        )
        assert pre_gateway_dispatch(click, gateway)["action"] == "skip"
        for _ in range(300):
            if (
                len(adapter.cards) == 2
                and conn.execute(
                    "SELECT count(*) FROM feishu_control_cards WHERE delivered_message_id IS NOT NULL"
                ).fetchone()[0]
                == 2
            ):
                break
            await asyncio.sleep(0.01)
        assert len(adapter.cards) == 2
        assert adapter.cards[1]["reply_to"] == "om_card"
        assert (
            conn.execute("SELECT mode FROM global_control_state").fetchone()[0]
            == "paused"
        )
        assert adapter.sent == []

    asyncio.run(exercise())


def test_card_binding_rejects_wrong_context_and_expiry(conn, config):
    setup_identity(config)

    def command(
        text, *, message="click", user="tenant-user", chat="oc_control", prompt=None
    ):
        return execute_control(
            conn,
            config,
            ControlMessage(user, chat, message, text, source_card_message_id=prompt),
            control_channel="feishu",
        )

    opened = command("mode", message="open")
    identifier = opened["card_id"]
    pause_index = next(
        index for index, value in enumerate(opened["commands"]) if value.endswith(" p")
    )
    click = f"card-action {identifier} {pause_index}"
    with pytest.raises(ControlError, match="not bound"):
        command(click, prompt="om_card")
    with pytest.raises(ControlError, match="binding mismatch"):
        command(f"card-bind {identifier} om_card", message="wrong-opening-message")
    command(f"card-bind {identifier} om_card", message="open")
    command(
        f"card-bind {identifier} om_card", message="open"
    )  # Same receipt may retry.
    with pytest.raises(ControlError, match="binding mismatch"):
        command(f"card-bind {identifier} om_other", message="open")
    for prompt in (None, "om_other"):
        with pytest.raises(ControlError, match="not bound"):
            command(click, prompt=prompt)
    with pytest.raises(ApprovalError):
        command(click, prompt="om_card", user="intruder")
    with pytest.raises(ApprovalError):
        command(click, prompt="om_card", chat="oc_other")
    assert command(click, prompt="om_card")["mode"] == "paused"
    with pytest.raises(RuntimeControlError, match="stale"):
        command(click, prompt="om_card")
    fresh = command("mode", message="fresh-open")
    command(f"card-bind {fresh['card_id']} om_fresh", message="fresh-open")
    conn.execute(
        "UPDATE feishu_control_cards SET expires_at='2000-01-01T00:00:00+00:00' WHERE card_id=?",
        (fresh["card_id"],),
    )
    with pytest.raises(ControlError, match="expiry"):
        command(f"card-action {fresh['card_id']} 0", prompt="om_fresh")


def test_offline_recovery_expires_historical_card_authority(conn, config):
    from k3_support.recovery_fence import fence

    setup_identity(config)
    message = ControlMessage("tenant-user", "oc_control", "open", "mode")
    opened = execute_control(conn, config, message, control_channel="feishu")
    execute_control(
        conn,
        config,
        ControlMessage(
            "tenant-user",
            "oc_control",
            "open",
            f"card-bind {opened['card_id']} om_card",
        ),
        control_channel="feishu",
    )
    result = fence(conn)
    assert result["feishu_cards_expired"] == 1
    assert (
        conn.execute(
            "SELECT delivered_message_id FROM feishu_control_cards"
        ).fetchone()[0]
        == "om_card"
    )
    with pytest.raises(ControlError, match="expiry"):
        execute_control(
            conn,
            config,
            ControlMessage(
                "tenant-user",
                "oc_control",
                "click",
                f"card-action {opened['card_id']} 0",
                source_card_message_id="om_card",
            ),
            control_channel="feishu",
        )
