from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class LarkError(RuntimeError):
    def __init__(self, message: str, *, error_type: str = "unknown", subtype: str | None = None, missing_scopes: list[str] | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.subtype = subtype
        self.missing_scopes = missing_scopes or []


@dataclass(frozen=True)
class CommandResult:
    data: Any
    identity: str | None
    notices: list[Any]
    meta: dict[str, Any] = field(default_factory=dict)


SAFE_ENV = {
    "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
    "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
}


def _capture(argv, *, timeout, env):
    from .bounded_cli import OutputLimitError, run
    try:
        return run(argv, timeout=timeout, env=env)
    except OutputLimitError as error:
        raise LarkError(str(error), error_type='output_limit', subtype='remote_result_unknown') from error
    except UnicodeDecodeError as error:
        raise LarkError('CLI returned invalid UTF-8; remote result unknown',
                        error_type='protocol', subtype='remote_result_unknown') from error


def _parse_error(stderr: str, returncode: int) -> LarkError:
    try:
        parsed = json.loads(stderr)
        envelope = parsed if isinstance(parsed, dict) and parsed.get("ok") is False else None
    except json.JSONDecodeError:
        envelope = None
    for line in reversed(stderr.splitlines()) if envelope is None else ():
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("ok") is False:
            envelope = candidate
            break
    if envelope:
        error = envelope.get("error") or {}
        return LarkError(
            str(error.get("message") or f"lark-cli exited {returncode}"),
            error_type=str(error.get("type") or "unknown"),
            subtype=error.get("subtype"),
            missing_scopes=list(error.get("missing_scopes") or []),
        )
    return LarkError(f"lark-cli exited {returncode}")


def run_json(argv: list[str], *, timeout: float = 30.0, executable: str = "lark-cli") -> CommandResult:
    if "--as" not in argv:
        raise ValueError("lark-cli identity must be explicit")
    env = os.environ.copy()
    env.update(SAFE_ENV)
    process = _capture(
        [executable, *argv],
        timeout=timeout,
        env=env,
    )
    if process.returncode != 0:
        raise _parse_error(process.stderr, process.returncode)
    try:
        envelope = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise LarkError("lark-cli returned invalid JSON", error_type="protocol") from exc
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        raise LarkError("lark-cli success envelope missing ok=true", error_type="protocol")
    meta = envelope.get('meta', {})
    if not isinstance(meta, dict):
        raise LarkError('lark-cli metadata must be an object', error_type='protocol')
    return CommandResult(envelope.get("data"), envelope.get("identity"), list(envelope.get("notices") or []), meta)


def run_mail_json(
    argv: list[str], *, timeout: float = 30.0, executable: str = "lark-cli"
) -> CommandResult:
    """Run a Mail shortcut that may print a CLI tip before its JSON result."""
    if "--as" not in argv:
        raise ValueError("lark-cli identity must be explicit")
    env = os.environ.copy()
    env.update(SAFE_ENV)
    process = _capture(
        [executable, *argv],
        timeout=timeout,
        env=env,
    )
    if process.returncode != 0:
        raise _parse_error(process.stderr, process.returncode)
    output = process.stdout.strip()
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        lines = output.splitlines()
        start = next(
            (
                index
                for index, line in enumerate(lines)
                if line.startswith("{")
                and all(
                    not prefix.strip() or prefix.startswith("tip:")
                    for prefix in lines[:index]
                )
            ),
            None,
        )
        if start is None:
            raise LarkError(
                "lark-cli returned invalid Mail JSON", error_type="protocol"
            )
        try:
            value = json.loads("\n".join(lines[start:]))
        except json.JSONDecodeError as exc:
            raise LarkError(
                "lark-cli returned invalid Mail JSON", error_type="protocol"
            ) from exc
    if not isinstance(value, dict):
        raise LarkError("lark-cli Mail result must be an object", error_type="protocol")
    if value.get("ok") is False:
        raise LarkError(
            str((value.get("error") or {}).get("message") or "Mail command failed"),
            error_type=str((value.get("error") or {}).get("type") or "unknown"),
            subtype=(value.get("error") or {}).get("subtype"),
            missing_scopes=list((value.get("error") or {}).get("missing_scopes") or []),
        )
    if value.get("ok") is True:
        meta = value.get('meta', {})
        if not isinstance(meta, dict):
            raise LarkError('lark-cli Mail metadata must be an object', error_type='protocol')
        return CommandResult(
            value.get("data"),
            value.get("identity"),
            list(value.get("notices") or []),
            meta,
        )
    return CommandResult(value, "user", [])


def normalize_bot_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("event must be an object")
    required = {"message_id", "chat_id", "chat_type", "sender_id", "create_time", "message_type", "content"}
    missing = required - set(value)
    if missing:
        raise ValueError(f"event is missing: {', '.join(sorted(missing))}")
    if value["chat_type"] not in {"p2p", "group"}:
        raise ValueError("event chat_type must be p2p or group")
    if not all(
        str(value[key]).strip() for key in ("message_id", "chat_id", "sender_id")
    ):
        raise ValueError("event lacks immutable coordinates")
    create_ms = str(value["create_time"])
    occurred = datetime.fromtimestamp(int(create_ms) / 1000, UTC).isoformat()
    content = value["content"]
    if isinstance(content, dict):
        content = content.get("text") or json.dumps(
            content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return {
        "source": "feishu_bot_im",
        "identity": "bot",
        "external_id": str(value["message_id"]),
        "sender_id": str(value["sender_id"]),
        "chat_id": str(value["chat_id"]),
        "thread_id": value.get("thread_id") or value.get("root_id"),
        "occurred_at": occurred,
        "payload": {
            "chat_type": value["chat_type"],
            "chat_name": value.get("chat_name"),
            "message_type": value["message_type"],
            "content": str(content or ""),
            "mentions": value.get("mentions") or [],
            "sender_name": value.get("sender_name"),
            "root_id": value.get("root_id"),
            "parent_id": value.get("parent_id"),
        },
    }


def normalize_mail_event(value: Any, mailbox: str = "me") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("mail event must be an object")
    if value.get("ok") is True:
        value = value.get("data")
    message = value.get("message") if isinstance(value, dict) else None
    if not isinstance(message, dict) or not message.get("message_id"):
        raise ValueError("mail event is missing message_id")
    internal_date = str(message.get("internal_date") or int(time.time() * 1000))
    sender = message.get("head_from") or {}
    occurred = datetime.fromtimestamp(int(internal_date) / 1000, UTC).isoformat()
    return {
        "source": "feishu_mail",
        "identity": "user",
        "external_id": f"{message['message_id']}:received",
        "sender_id": sender.get("mail_address"),
        "chat_id": mailbox,
        "thread_id": message.get("thread_id"),
        "occurred_at": occurred,
        "payload": {
            "message_id": message["message_id"],
            "thread_id": message.get("thread_id"),
            "subject": message.get("subject"),
            "head_from": {"name": sender.get("name"), "mail_address": sender.get("mail_address")},
            "folder_id": message.get("folder_id"),
            "label_ids": message.get("label_ids") or [],
            "body_preview": message.get("body_preview"),
            "body_plain_text": message.get("body_plain_text"),
            "priority_type": message.get("priority_type"),
            "priority_text": message.get("priority_text"),
            "security_level": message.get("security_level"),
            "attachments": message.get("attachments") or [],
            "internal_date": internal_date,
            "message_state": message.get("message_state"),
        },
    }


class EventConsumer:
    """Supervise an event stream with independent ready and bounded diagnostics."""

    def __init__(self, *, executable: str = "lark-cli", ready_timeout: float = 30.0):
        self.executable = executable
        self.ready_timeout = ready_timeout
        self.process = None
        self._diagnostics = None
        self._stderr_thread = None
        self._stderr_done = threading.Event()
        self._stop_reading = threading.Event()
        self._session_owned = False

    @property
    def warnings(self):
        return self._diagnostics.warnings if self._diagnostics is not None else []

    @property
    def diagnostic_stats(self):
        return self._diagnostics.stats if self._diagnostics is not None else {}

    def start(self, event_key: str = "im.message.receive_v1", identity: str = "bot") -> None:
        from .event_diagnostics import Diagnostics
        if not isinstance(event_key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', event_key):
            raise ValueError('invalid event key')
        if self.ready_timeout <= 0:
            raise ValueError('positive ready timeout required')
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError('consumer already started')
        if self.process is not None:
            self.stop(timeout=0.5)
        self._diagnostics = Diagnostics(event_key)
        self._stderr_done.clear()
        self._stop_reading.clear()
        env = os.environ.copy()
        env.update(SAFE_ENV)
        self.process = subprocess.Popen(
            [self.executable, "event", "consume", event_key, "--as", identity],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=False, env=env, start_new_session=True,
        )
        self._session_owned = True
        process = self.process
        diagnostics = self._diagnostics

        def drain_stderr():
            selector = selectors.DefaultSelector()
            try:
                selector.register(process.stderr, selectors.EVENT_READ)
                while not self._stop_reading.is_set():
                    if not selector.select(0.1):
                        continue
                    chunk = os.read(process.stderr.fileno(), 4096)
                    if not chunk:
                        break
                    diagnostics.feed(chunk)
            finally:
                diagnostics.finish()
                selector.close()
                self._stderr_done.set()

        self._stderr_thread = threading.Thread(target=drain_stderr, name="lark-event-stderr", daemon=True)
        self._stderr_thread.start()
        deadline = time.monotonic() + self.ready_timeout
        while True:
            if diagnostics.ready.is_set():
                return
            if self._stderr_done.is_set() or process.poll() is not None:
                # Process exit can race with draining its final ready line.
                self._stderr_done.wait(min(0.5, max(0, deadline-time.monotonic())))
                if diagnostics.ready.is_set():
                    return
                self.stop(timeout=0.5)
                raise _parse_error('\n'.join(self.warnings), process.returncode or 1)
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                self.stop(timeout=0.5)
                raise LarkError("event consumer did not emit ready marker", error_type="timeout")
            diagnostics.ready.wait(min(remaining, 0.05))

    def events(self) -> Iterator[dict[str, Any]]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("consumer is not started")
        # A protocol-sized bound also prevents a malformed event line from
        # allocating an arbitrarily large buffer while preserving JSON bytes.
        while True:
            line = self.process.stdout.readline(2*1024*1024+1)
            if not line:
                return
            if len(line) > 2*1024*1024:
                self.stop(timeout=0.5)
                raise LarkError('event line exceeded its byte budget', error_type='protocol')
            if line.strip():
                yield json.loads(line)

    def stop(self, timeout: float = 10.0) -> None:
        if self.process is None:
            return
        from .bounded_cli import signal_owned_process
        process = self.process
        forced = False
        try:
            if process.poll() is None:
                if self._session_owned:
                    signal_owned_process(process, signal.SIGTERM)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=max(0.001, timeout))
                except subprocess.TimeoutExpired:
                    forced = True
                    if self._session_owned:
                        signal_owned_process(process, signal.SIGKILL)
                    else:
                        process.kill()
                    try:
                        process.wait(timeout=max(1.0, min(timeout, 5.0)))
                    except subprocess.TimeoutExpired as error:
                        raise LarkError('event consumer ignored SIGKILL', error_type='shutdown') from error
        finally:
            if self._session_owned:
                signal_owned_process(process, signal.SIGKILL)
                self._session_owned = False
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=0.5)
                self._stop_reading.set()
                self._stderr_thread.join(timeout=0.5)
            for name in ('stdin', 'stdout', 'stderr'):
                stream = getattr(process, name, None)
                if stream is not None:
                    stream.close()
        if forced:
            raise LarkError('event consumer ignored SIGTERM and was killed', error_type='shutdown')


def mail_preflight(*, executable: str = "lark-cli") -> dict[str, Any]:
    try:
        result = run_mail_json(
            [
                "mail",
                "+triage",
                "--folder",
                "INBOX",
                "--max",
                "1",
                "--format",
                "json",
                "--as",
                "user",
                "--dry-run",
            ],
            timeout=30,
            executable=executable,
        )
    except LarkError as exc:
        return {
            "ok": False,
            "state": "blocked_auth" if exc.error_type in {"authorization", "authentication"} else "failed",
            "error_type": exc.error_type,
            "subtype": exc.subtype,
            "missing_scopes": exc.missing_scopes,
            "message": str(exc),
        }
    return {"ok": True, "state": "ready", "identity": result.identity}
