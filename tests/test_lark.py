from __future__ import annotations

import subprocess

import pytest

from k3_support.lark import (
    EventConsumer,
    _parse_error,
    normalize_bot_event,
    normalize_mail_event,
    run_mail_json,
)


def test_bot_event_uses_message_id_and_plain_content():
    value = {
        "message_id": "om_123",
        "chat_id": "oc_123",
        "chat_type": "group",
        "sender_id": "ou_123",
        "create_time": "1788220800000",
        "message_type": "text",
        "content": "K3 怎么进入 fastboot？",
        "sender_name": "飞书同事",
        "chat_name": "K3 技术支持",
        "mentions": [{"id": "ou_owner"}],
    }
    normalized = normalize_bot_event(value)
    assert normalized["external_id"] == "om_123"
    assert normalized["payload"]["content"] == value["content"]
    assert normalized["payload"]["sender_name"] == "飞书同事"
    assert normalized["payload"]["chat_name"] == "K3 技术支持"
    assert normalized["occurred_at"].endswith("+00:00")


def test_bot_event_normalizes_object_content_and_rejects_unknown_chat_type():
    value = {
        "message_id": "om_object",
        "chat_id": "oc_123",
        "chat_type": "p2p",
        "sender_id": "ou_123",
        "create_time": "1788220800000",
        "message_type": "text",
        "content": {"text": "K3 怎么启动？"},
    }
    assert normalize_bot_event(value)["payload"]["content"] == "K3 怎么启动？"
    value["chat_type"] = "unknown"
    with pytest.raises(ValueError, match="chat_type"):
        normalize_bot_event(value)


def test_mail_normalization_keeps_plain_text_for_private_ai_summary():
    value = {
        "ok": True,
        "data": {
            "message": {
                "message_id": "mail-1",
                "thread_id": "thread-1",
                "subject": "K3 test",
                "head_from": {"name": "Alice", "mail_address": "alice@example.test"},
                "body_preview": "Please inspect",
                "body_plain_text": "Please inspect the complete K3 build report.",
                "body_html": "must not be copied at ingress",
                "internal_date": "1788220800000",
                "label_ids": ["IMPORTANT"],
            }
        },
    }
    normalized = normalize_mail_event(value)
    assert normalized["external_id"] == "mail-1:received"
    assert "body_html" not in normalized["payload"]
    assert normalized["payload"]["body_preview"] == "Please inspect"
    assert normalized["payload"]["body_plain_text"] == "Please inspect the complete K3 build report."


def test_mail_normalization_does_not_require_sender_address():
    value = {
        "ok": True,
        "data": {
            "message": {
                "message_id": "mail-without-address",
                "subject": "K3 status",
                "head_from": {"name": "Alice"},
                "body_preview": "Build completed",
                "internal_date": "1788220800000",
            }
        },
    }

    normalized = normalize_mail_event(value)

    assert normalized["sender_id"] is None
    assert normalized["payload"]["head_from"] == {
        "name": "Alice",
        "mail_address": None,
    }


def test_structured_lark_error_extracts_missing_scopes():
    error = _parse_error(
        '{"ok":false,"error":{"type":"authorization","subtype":"missing_scope",'
        '"message":"permission denied","missing_scopes":["mail:event"]}}',
        3,
    )
    assert error.error_type == "authorization"
    assert error.subtype == "missing_scope"
    assert error.missing_scopes == ["mail:event"]


def test_pretty_printed_lark_error_is_parsed_as_one_envelope():
    error = _parse_error(
        '{\n  "ok": false,\n  "error": {\n    "type": "authorization",\n'
        '    "subtype": "missing_scope",\n    "message": "denied",\n'
        '    "missing_scopes": ["search:message"]\n  }\n}',
        3,
    )
    assert error.error_type == "authorization"
    assert error.missing_scopes == ["search:message"]


def test_mail_json_runner_accepts_known_cli_tip_prefix(tmp_path):
    executable = tmp_path / "fake-mail"
    executable.write_text(
        "#!/bin/sh\n"
        "echo 'tip: run profile first'\n"
        "echo '{\"ok\":true,\"identity\":\"user\",\"data\":{\"messages\":[]}}'\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)

    result = run_mail_json(
        ["mail", "+messages", "--as", "user"], executable=str(executable)
    )

    assert result.identity == "user"
    assert result.data == {"messages": []}


def test_event_consumer_waits_for_ready_and_stops_with_sigterm(tmp_path):
    executable = tmp_path / "fake-lark"
    executable.write_text(
        "#!/bin/sh\n"
        "echo 'startup diagnostic' >&2\n"
        "echo '[event] ready event_key=im.message.receive_v1' >&2\n"
        "echo '{\"message_id\":\"om_1\"}'\n"
        "echo 'runtime warning' >&2\n"
        "trap 'exit 0' TERM INT\n"
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    consumer = EventConsumer(executable=str(executable), ready_timeout=2)
    consumer.start()
    event = next(consumer.events())
    assert event == {"message_id": "om_1"}
    consumer.stop(timeout=2)
    assert consumer.process is not None and consumer.process.returncode is not None
    assert "startup diagnostic" in consumer.warnings


def test_event_consumer_kills_process_that_ignores_sigterm():
    class Process:
        returncode = None

        def __init__(self):
            self.terminated = False
            self.killed = False
            self.waits = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(cmd="fake-lark", timeout=timeout)
            return self.returncode

    process = Process()
    consumer = EventConsumer()
    consumer.process = process  # type: ignore[assignment]

    with pytest.raises(Exception, match="was killed"):
        consumer.stop(timeout=0.01)
    assert process.terminated is True
    assert process.killed is True
