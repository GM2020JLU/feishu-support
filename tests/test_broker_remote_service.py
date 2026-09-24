import json
import os
import threading

import pytest
import yaml
from test_broker_execution_contract import contract

from k3_support.broker_remote_service import run


def test_consumer_uses_private_existing_database_and_contract(conn, config, tmp_path, monkeypatch):
    config.path.write_text(yaml.safe_dump(config.raw))
    directory = tmp_path / "contract"
    directory.mkdir(mode=0o755)
    (directory / "execution-contract.json").write_text(json.dumps(contract()))
    calls = []
    def consume(db, cfg, **kw):
        assert cfg.source_config_guard is not None
        assert cfg.source_config_guard().raw == cfg.raw
        assert not db.in_transaction
        assert kw["contract_reader"]().model == "gpt-5.6-sol"
        calls.append(True)
        return {"state": "idle"}
    monkeypatch.setattr("k3_support.broker_remote_service.run_one", consume)
    options = {"config_path": config.path, "contract_directory": directory,
                   "worker_uid": os.geteuid()+1, "watch": False, "stop_event": threading.Event()}
    assert run(**options) == {"state": "idle"}
    assert calls == [True]
    options["stop_event"].set()
    assert run(**options) == {"state": "stopped"}
    assert calls == [True]
    with pytest.raises(ValueError):
        run(**{**options, "worker_uid": os.geteuid()})
    conn.execute("DELETE FROM schema_migrations WHERE version=75")
    with pytest.raises(ValueError, match="migration"):
        run(**options)
