import json
import os
import signal
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from test_broker_claim import queued
from test_broker_execution_contract import contract
from test_broker_start_observation import synthetic_observer
from test_broker_worker_cli import argv, settings
from test_review import active_config, result_text
from test_semantic_budget import policy

from k3_support import broker_worker_cli as worker
from k3_support.broker_client import request_at
from k3_support.broker_execution_contract import load_at
from k3_support.broker_identity import Peer
from k3_support.broker_listener import serve
from k3_support.broker_worker import process_executor
from k3_support.db import connect
from k3_support.execution_stop import apply, preview, status


@pytest.mark.parametrize("outcome", ["report", "cancel", "signal", "contract_match", "contract_mismatch"])
def test_entrypoint_real_socket_process_and_exit(conn, config, tmp_path, monkeypatch, capsys, outcome):
    queued(conn)
    monkeypatch.setattr("k3_support.broker_connection.observe_running", synthetic_observer)
    cfg = active_config(config)
    case_id = conn.execute("SELECT case_id FROM jobs").fetchone()[0]
    values = settings(tmp_path)
    worker_uid = os.geteuid()+1
    contract_reader = None
    if outcome.startswith("contract_"):
        policy(conn)
        directory = tmp_path / "contract"
        directory.mkdir(mode=0o755)
        path = directory / "execution-contract.json"
        path.write_text(json.dumps(contract()))
        path.chmod(0o644)
        values["contract_directory"] = str(directory)
        # Same-UID fixture: only owner identities are adapted. Both readers
        # still open/validate/hash actual files; this is not an OS ACL canary.
        monkeypatch.setattr(worker, "load_at", lambda fd, **_: load_at(
            fd, control_uid=os.geteuid(), worker_uid=worker_uid))

        def contract_reader():
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                return load_at(fd, control_uid=os.geteuid(), worker_uid=worker_uid)
            finally:
                os.close(fd)
    # Actual business protocol and child process, but distinct-UID checks are
    # simulated here. This is not an OS identity or production model canary.
    monkeypatch.setattr("k3_support.broker_identity.authenticate_worker",
                        lambda *a, **kw: Peer(pid=42, uid=worker_uid, gid=worker_uid))
    monkeypatch.setattr("k3_support.broker_client.authenticate_worker",
                        lambda *a, **kw: Peer(pid=43, uid=values["control_uid"], gid=0))
    marker = Path(values["workspace_root"]) / values["claim_request_id"] / "child.pid"
    code = "import json,os,sys; v=json.load(sys.stdin); assert 'lease_token' not in v; assert 'CONTROL_SECRET' not in os.environ; print("+repr(result_text(case_id))+")"
    if outcome in {"cancel", "signal"}:
        code = "import os,pathlib,time; pathlib.Path('child.pid').write_text(str(os.getpid())); time.sleep(30)"
    monkeypatch.setenv("CONTROL_SECRET", "private-control-secret")
    def adapter(**kwargs):
        if outcome.startswith("contract_"):
            assert kwargs["contract"].provider == "fixture"
        if outcome == "contract_mismatch":
            # Change protected configuration after worker loaded its snapshot.
            path.write_text(json.dumps({**contract(), "provider": "replacement"}))
            def forbidden(*_args, **_kwargs):
                pytest.fail("mismatched contract must not invoke the executor")
            return forbidden
        return process_executor(argv=[sys.executable, "-c", code], cwd=kwargs["workdir"],
                                env=kwargs["environment"], timeout=2, heartbeat_interval=0.05)
    monkeypatch.setattr(worker, "executor", adapter)
    stop = threading.Event()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(values["socket_path"])
        listener.listen(4)
        def server():
            db = connect(config.database_path)
            try:
                return serve(db, listener, config=cfg, worker_uid=worker_uid,
                             control_key=b"t"*32, stop_event=stop, contract_reader=contract_reader)
            finally:
                db.close()
        def transport(path, request, **kwargs):
            if outcome == "contract_match" and request["method"] == "result":
                attempt = conn.execute("SELECT state,charged,reserved FROM model_budget_attempts").fetchone()
                assert attempt["state"] == "dispatched" and attempt["charged"] == attempt["reserved"] > 0
            if marker.exists() and request["method"] == "renew":
                if outcome == "cancel":
                    current = preview(conn, job_id="job-1")
                    receipt = apply(conn, job_id="job-1", binding_digest=current["binding_digest"],
                                    request_id=str(uuid4()), actor_id="synthetic-controller")
                    assert receipt["accepted"] and not receipt["process_exit_verified"]
                elif outcome == "signal":
                    os.kill(os.getpid(), signal.SIGTERM)
            return request_at(path, request, **kwargs)
        monkeypatch.setattr(worker, "request_at", transport)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(server)
            try:
                result = worker.main(argv(values))
                if outcome in {"report", "contract_match"}:
                    assert result == 0
                    assert "report_received" in capsys.readouterr().out
                    # Same command/request can recover a claim response, but
                    # cannot obtain a second execution start after completion.
                    assert worker.main(argv(values)) == 1
                elif outcome == "contract_mismatch":
                    assert result == 1
                    assert not marker.exists()
                else:
                    assert result == (143 if outcome == "signal" else 1)
                    with pytest.raises(ProcessLookupError):
                        os.kill(int(marker.read_text()), 0)
            finally:
                stop.set()
                future.result(timeout=5)
    assert conn.execute("SELECT count(*) FROM broker_execution_starts").fetchone()[0] == int(outcome != "contract_mismatch")
    assert conn.execute("SELECT count(*) FROM broker_results").fetchone()[0] == int(outcome in {"report", "contract_match"})
    assert conn.execute("SELECT count(*) FROM broker_execution_contracts").fetchone()[0] == int(outcome == "contract_match")
    assert conn.execute("SELECT count(*) FROM broker_budget_attempts").fetchone()[0] == int(outcome == "contract_match")
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == ("cancelled" if outcome == "cancel" else "running")
    if outcome == "cancel":
        assert conn.execute("SELECT count(*) FROM execution_stop_requests").fetchone()[0] == 1
        # Local process death is not yet a trusted control-side exit receipt.
        observed = status(conn, job_id="job-1")
        assert observed["requested"] and observed["process"] == "unverified"
