import json
import os
import threading

import pytest
import yaml
from test_broker_execution_contract import contract

from k3_support import broker_board_service
from k3_support.broker_remote_service import run


def test_board_entry_uses_shared_consumer_lifecycle(monkeypatch):
    calls = []
    def lifecycle(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0
    monkeypatch.setattr(broker_board_service, "consumer_main", lifecycle)
    assert broker_board_service.main(["--help"]) == 0
    assert calls[0][1]["consumer"] is broker_board_service.run_one


@pytest.mark.parametrize("state,dispatch", [("idle", True), ("cleanup_not_configured", True),
    ("cleanup_unknown", False), ("board_cleaned", False), ("occupied", False), ("stopped", False)])
def test_board_entry_checks_cleanup_before_dispatching_actions(monkeypatch, state, dispatch):
    order = []
    def cleanup(*args, **kwargs):
        order.append("cleanup")
        return {"state": state}
    def action(*args, **kwargs):
        order.append("action")
        return {"state": "action-result"}
    monkeypatch.setattr(broker_board_service, "run_cleanup", cleanup)
    monkeypatch.setattr(broker_board_service, "run_action", action)
    result = broker_board_service.run_one(None, None, contract_reader=lambda: None)
    assert order == (["cleanup", "action"] if dispatch else ["cleanup"])
    assert result["state"] == ("action-result" if dispatch else state)


def test_shared_service_validates_identity_and_db_before_custom_consumer(conn, config, tmp_path):
    config.path.write_text(yaml.safe_dump(config.raw))
    directory = tmp_path / "contracts"
    directory.mkdir(mode=0o755)
    (directory / "execution-contract.json").write_text(json.dumps(contract()))
    calls = []
    def consumer(db, cfg, **kwargs):
        assert not db.in_transaction
        assert kwargs["contract_reader"]().model == "gpt-5.6-sol"
        calls.append(True)
        return {"state": "idle"}
    result = run(config_path=config.path, contract_directory=directory, worker_uid=os.geteuid()+1,
                 watch=False, stop_event=threading.Event(), consumer=consumer)
    assert result == {"state": "idle"} and calls == [True]
