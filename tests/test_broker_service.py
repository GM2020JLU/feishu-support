import os
import socket
import threading
from types import SimpleNamespace

import pytest
import yaml

from k3_support.broker_service import run


def test_service_assembles_existing_resources_without_migration(conn, config, tmp_path, monkeypatch):
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    private = tmp_path / "keys"
    private.mkdir(mode=0o700)
    key = private / "broker.key"
    key.write_bytes(b"t" * 32)
    key.chmod(0o600)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(tmp_path / "service.sock"))
    listener.listen()
    monkeypatch.setattr("k3_support.broker_service.take_listener", lambda: listener)
    monkeypatch.setattr("k3_support.broker_service.UnitReferences", lambda: SimpleNamespace(close=lambda: None))
    stopped = threading.Event()
    stopped.set()
    before = list(conn.iterdump())
    assert run(config_path=config.path, key_directory=private, worker_uid=os.geteuid()+1,
               stop_event=stopped) == {"connections": 0, "transport_rejections": 0}
    assert list(conn.iterdump()) == before
    assert listener.fileno() == -1


def test_service_rejects_old_schema_without_migrating(conn, config, tmp_path, monkeypatch):
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    private = tmp_path / "keys"
    private.mkdir(mode=0o700)
    key = private / "broker.key"
    key.write_bytes(b"t" * 32)
    key.chmod(0o600)
    conn.execute("DELETE FROM schema_migrations WHERE version=(SELECT max(version) FROM schema_migrations)")
    before = list(conn.iterdump())
    def forbidden():
        raise AssertionError("must reject schema before listener activation")
    monkeypatch.setattr("k3_support.broker_service.take_listener", forbidden)
    with pytest.raises(ValueError, match="migration"):
        run(config_path=config.path, key_directory=private, worker_uid=os.geteuid()+1,
            stop_event=threading.Event())
    assert list(conn.iterdump()) == before
