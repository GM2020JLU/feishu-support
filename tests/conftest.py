from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

from k3_support.config import Config, validate_config
from k3_support.db import connect, migrate


@pytest.fixture(autouse=True)
def no_live_transports(monkeypatch, tmp_path_factory):
    """Fail on forgotten transport doubles, before starting a real command.

    This is an accidental-test-side-effect guard, not a hostile-code sandbox.
    Child-interpreter integration tests must provide their own isolated home,
    injected transports and audit hooks (the installed-release verifier does).
    """
    for variable in list(os.environ):
        if any(marker in variable.upper() for marker in ("TOKEN", "API_KEY", "SECRET", "PASSWORD", "AUTHORIZATION", "COOKIE")):
            monkeypatch.delenv(variable, raising=False)
    original_popen = subprocess.Popen
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def guarded_popen(args, *positional, **kwargs):
        if kwargs.get("shell") or isinstance(args, (str, bytes)):
            raise AssertionError("tests must not launch an unreviewed shell command")
        argv = [os.fsdecode(value) for value in args]
        name = Path(argv[0]).name
        allowed = (
            name.startswith("python")
            or (name == "codex" and argv[1:3] == ["execpolicy", "check"])
            or (name == "systemd-analyze" and "verify" in argv[1:])
            or (name == "uv" and "--offline" in argv[1:])
        )
        if name == "git":
            tail = argv[1:]
            if tail[:1] == ["-C"]:
                tail = tail[2:]
            allowed = bool(tail) and tail[0] in {
                "init", "config", "add", "commit", "rev-parse", "show",
                "status", "log", "diff", "cat-file", "rev-list", "merge-base",
                "checkout", "branch", "merge",
                "remote", "ls-files",
            }
            if tail[:1] == ["ls-remote"]:
                allowed = any(
                    Path(value).is_absolute() and Path(value).is_dir()
                    and Path(value).resolve().is_relative_to(tmp_path_factory.getbasetemp().resolve())
                    for value in tail[1:]
                )
        if name.startswith("fake-"):
            fixture_command = Path(argv[0])
            allowed = (
                fixture_command.is_absolute() and not fixture_command.is_symlink()
                and fixture_command.is_file()
                and fixture_command.resolve().is_relative_to(tmp_path_factory.getbasetemp().resolve())
            )
        if name == "node" and len(argv) == 4:
            # Only the packaged stdin bridge with a synthetic loader; never dsh.
            bridge = Path(__file__).parents[1] / "src/k3_support/agent_bridges/dsh-stdin.mjs"
            loader = Path(argv[2])
            allowed = (
                Path(argv[1]).resolve() == bridge.resolve()
                and loader.is_file() and not loader.is_symlink()
                and loader.resolve().is_relative_to(tmp_path_factory.getbasetemp().resolve())
            )
        if name == "k3-supportctl" and argv[1:2] == ["--config"] and len(argv) > 3:
            fixture_config = Path(argv[2]).resolve()
            allowed = (
                fixture_config.is_file()
                and fixture_config.is_relative_to(tmp_path_factory.getbasetemp().resolve())
                and argv[3] in {"control", "control-callback", "control-panel-bind", "workbench", "mail-summary-show"}
            )
        if not allowed:
            raise AssertionError(
                f"live command blocked in tests: {name}; inject a transport double"
            )
        return original_popen(args, *positional, **kwargs)

    def no_network(sock, *args, **kwargs):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("live network blocked in tests; inject a transport double")
        return original_connect(sock, *args, **kwargs)

    original_connect = socket.socket.connect
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(socket.socket, "connect", no_network)


def config_data(tmp_path: Path, *, mode: str = "shadow") -> dict:
    return {
        "schema_version": 1,
        "mode": mode,
        "timezone": "Asia/Shanghai",
        "work_hours": {"start": "09:00", "end": "18:00"},
        "paths": {"data_dir": str(tmp_path), "database": str(tmp_path / "support.db")},
        "features": {
            "shadow_reply": True,
            "auto_faq": False,
            "codex": False,
            "board": False,
            "wip_push": False,
            "mail": False,
            "calendar": False,
            "base_sync": False,
        },
        "policy": {
            "auto_reply_confidence": 0.85,
            "raw_retention_days": 30,
            "board_alias": "board1",
            "board_lease_max_minutes": 240,
            "push_approval_minutes": 30,
        },
        "identity": {
            "telegram_control_user_id": "owner-user",
            "telegram_control_chat_id": "owner-chat",
            "feishu_owner_open_id": None,
            "feishu_p0_chat_id": None,
        },
        "scope": {"technical_chat_ids": [], "auto_reply_chat_ids": []},
        "repositories": {},
        "base": {
            "app_token": None,
            "cases_table_id": None,
            "knowledge_table_id": None,
            "mail_table_id": None,
            "health_table_id": None,
        },
    }


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "support.db")
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def config(tmp_path):
    data = validate_config(config_data(tmp_path))
    return Config(data, tmp_path / "config.yaml")
