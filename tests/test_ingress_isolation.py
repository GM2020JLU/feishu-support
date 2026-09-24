from __future__ import annotations

import copy
import json
import threading
import time
from itertools import pairwise

import pytest

from k3_support.config import Config
from k3_support.lark import CommandResult
from k3_support.services import _ingress_poll_stream


def test_slow_mail_does_not_delay_chat_operator_poll_cadence(conn, config):
    stop = threading.Event()
    release_mail = threading.Event()
    mail_entered = threading.Event()
    enough_chat = threading.Event()
    chat_starts = []
    connections = {}

    def slow_mail(argv):
        mail_entered.set()
        assert release_mail.wait(3)
        return CommandResult({}, "user", [])

    def mail_poller(own, cfg, *, runner, should_stop):
        connections["mail"] = (own, threading.get_ident())
        runner(["mail"])
        pytest.fail("a stopped slow read may not advance a mail cursor")

    def chat_poller(own, cfg, *, runner):
        connections["chat"] = (own, threading.get_ident())
        chat_starts.append(time.monotonic())
        runner(["chat-and-operator"])
        if len(chat_starts) >= 4:
            enough_chat.set()
        return {"state": "ready", "operator_activities": 1}

    def start(component, poller, runner):
        thread = threading.Thread(target=_ingress_poll_stream, kwargs={
            "cfg": config, "component": component, "poller": poller, "runner": runner,
            "stop_event": stop, "interval_seconds": 0.03, "emit": lambda *args, **kwargs: None,
        })
        thread.start()
        return thread

    mail = start("ingress_mail", mail_poller, slow_mail)
    chat = start("ingress_poll", chat_poller, lambda _: CommandResult({}, "user", []))
    try:
        assert mail_entered.wait(1)
        assert enough_chat.wait(1)
        assert not release_mail.is_set()
        assert connections["mail"][0] is not connections["chat"][0]
        assert connections["mail"][1] != connections["chat"][1]
        assert max(after - before for before, after in pairwise(chat_starts)) < 0.2
        assert conn.execute("SELECT status FROM service_state WHERE component='ingress_poll'").fetchone()[0] == "ready"
    finally:
        stop.set()
        chat.join(timeout=1)
        release_mail.set()
        mail.join(timeout=1)
    assert not chat.is_alive() and not mail.is_alive()


def test_catchup_budget_is_healthy_but_protocol_error_is_not(conn, config):
    for state, expected in (("catching_up", "ready"), ("degraded", "failed")):
        _ingress_poll_stream(
            config, component="ingress_mail", poller=lambda *args, state=state, **kwargs: {
                "state": state, "complete": False, "pages": 4,
            }, runner=lambda _: pytest.fail("fixture has no external read"),
            stop_event=threading.Event(), once=True, emit=lambda *args, **kwargs: None,
        )
        row = conn.execute("SELECT status,detail_json FROM service_state WHERE component='ingress_mail'").fetchone()
        assert row[0] == expected
        assert json.loads(row[1])["state"] == state


def test_mail_failure_is_local_and_never_overwrites_chat_health(conn, config):
    def fail(*args, **kwargs):
        raise RuntimeError("fixture provider failed")

    for component, poller in (
        ("ingress_poll", lambda *args, **kwargs: {"state": "ready"}),
        ("ingress_mail", fail),
    ):
        _ingress_poll_stream(
            config, component=component, poller=poller,
            runner=lambda _: pytest.fail("fixture has no external read"),
            stop_event=threading.Event(), once=True, emit=lambda *args, **kwargs: None,
        )
    status = {row[0]: row[1] for row in conn.execute("SELECT component,status FROM service_state")}
    assert status["ingress_poll"] == "ready"
    assert status["ingress_mail"] == "failed"


def test_ingress_supervisor_acknowledges_stop_without_waiting_for_slow_mail(
    conn, config, monkeypatch,
):
    from k3_support import services

    raw = copy.deepcopy(config.raw)
    raw["features"]["mail"] = True
    cfg = Config(raw, config.path)
    monkeypatch.setattr(services, "load_config", lambda _: cfg)
    monkeypatch.setattr(services.signal, "signal", lambda *args: None)
    monkeypatch.setattr("sys.argv", ["fixture-ingress", "--once"])
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    received_stop = []

    def stream(**kwargs):
        if kwargs["component"] == "ingress_mail":
            entered.set()
            assert release.wait(3)
            received_stop.append(kwargs["stop_event"].is_set())
            finished.set()

    monkeypatch.setattr(services, "_ingress_poll_stream", stream)

    def request_stop():
        assert entered.wait(1)
        services.Stop.requested = True

    request = threading.Thread(target=request_stop)
    request.start()
    started = time.monotonic()
    try:
        services.ingress_main()
        assert time.monotonic() - started < 1.5
        assert not release.is_set() and not finished.is_set()
    finally:
        release.set()
        request.join(timeout=1)
        assert finished.wait(1)
        services.Stop.requested = False
    assert received_stop == [True]


def test_bot_only_ingress_does_not_start_user_or_mail_pollers(config, monkeypatch):
    from k3_support import services

    raw = copy.deepcopy(config.raw)
    raw["features"]["mail"] = True
    cfg = Config(raw, config.path)
    monkeypatch.setattr(services, "load_config", lambda _: cfg)
    monkeypatch.setattr(services.signal, "signal", lambda *args: None)
    monkeypatch.setattr("sys.argv", ["fixture-ingress", "--bot-only"])
    started = threading.Event()

    class BotConsumer:
        def __init__(self, **kwargs):
            pass

        def start(self):
            started.set()
            services.Stop.requested = True

        def events(self):
            return iter(())

        def stop(self, **kwargs):
            pass

    monkeypatch.setattr(services, "EventConsumer", BotConsumer)
    monkeypatch.setattr(
        services, "_ingress_poll_stream",
        lambda **kwargs: pytest.fail("bot-only ingress started a user or mail poller"),
    )
    try:
        services.ingress_main()
        assert started.wait(1)
    finally:
        services.Stop.requested = False
