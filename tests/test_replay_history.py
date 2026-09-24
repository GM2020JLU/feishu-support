import fcntl
import json
import os
import sqlite3

import pytest
from test_routing import active_config, route_value
from test_workflow_replay import event

from k3_support import replay_history as history


def request(config):
    return {
        "config": active_config(config).raw,
        "event": event(),
        "proposal": route_value(
            "owner_decision",
            issue_type="request",
            requires_owner_judgment=True,
            reason_codes=["requires_commitment"],
        ),
    }


def test_snapshot_exec_uses_private_memory_only(conn, config, monkeypatch):
    conn.execute("CREATE TABLE replay_wal_canary(value TEXT)")
    conn.execute("INSERT INTO replay_wal_canary VALUES ('committed WAL value')")

    def process(**kw):
        path = f'/proc/self/fd/{kw["pass_fds"][0]}'
        with open(path, "rb") as stream:
            data = stream.read()
        check = sqlite3.connect(":memory:")
        try:
            check.deserialize(data)
            assert (
                check.execute("SELECT value FROM replay_wal_canary").fetchone()[0]
                == "committed WAL value"
            )
        finally:
            check.close()
        return json.dumps(history.execute_snapshot(json.loads(kw["stdin"]), path))

    monkeypatch.setattr(history, "run_process", process)
    before = conn.serialize()
    result = history.run_snapshot_replay(config.database_path, request(config))
    assert result["report"]["result"]["route"]["route"] == "owner_decision"
    assert result["model_invoked"] is False
    assert conn.serialize() == before


def test_transfer_is_sealed_and_descriptor_closed(conn, config, monkeypatch):
    paths = []

    def process(**kw):
        argv = kw["argv"]
        path = f'/proc/self/fd/{kw["pass_fds"][0]}'
        paths.append(path)
        with open(path, "rb") as stream:
            assert stream.read(16) == b"SQLite format 3\0"
            seals = fcntl.fcntl(stream, history.F_GET_SEALS)
            assert seals & history.F_SEAL_WRITE
        with pytest.raises(OSError):
            fd = os.open(path, os.O_WRONLY)
            try:
                os.write(fd, b"x")
            finally:
                os.close(fd)
        assert str(config.database_path) not in argv
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(history, "run_process", process)
    before = conn.serialize()
    with pytest.raises(TimeoutError):
        history.run_snapshot_replay(config.database_path, request(config))
    assert conn.serialize() == before
    assert not os.path.exists(paths[0])


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 301, True])
def test_bad_timeout_before_snapshot(config, monkeypatch, timeout):
    monkeypatch.setattr(
        history,
        "replay_snapshot",
        lambda *a, **k: pytest.fail("unexpected source access"),
    )
    with pytest.raises(ValueError):
        history.run_snapshot_replay(
            config.database_path, request(config), timeout=timeout
        )
