import os
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_broker_claim import queued
from test_broker_start_observation import synthetic_observer
from test_review import active_config, result_text

from k3_support.broker_client import request_at
from k3_support.broker_identity import Peer
from k3_support.broker_listener import serve
from k3_support.broker_protocol import ProtocolError
from k3_support.broker_worker import process_executor, run_one
from k3_support.db import connect


@pytest.mark.parametrize("cancel", [False, True])
def test_real_transport_process_and_report_pipeline(conn, config, tmp_path, monkeypatch, cancel):
    queued(conn)
    monkeypatch.setattr("k3_support.broker_connection.observe_running", synthetic_observer)
    cfg = active_config(config)
    case_id = conn.execute("SELECT case_id FROM jobs WHERE job_id='job-1'").fetchone()[0]
    worker_uid = os.geteuid() + 1
    # This test exercises transport/business/process integration, not UID isolation.
    monkeypatch.setattr("k3_support.broker_identity.authenticate_worker",
                        lambda *a, **kw: Peer(pid=42, uid=worker_uid, gid=worker_uid))
    monkeypatch.setattr("k3_support.broker_client.authenticate_worker",
                        lambda *a, **kw: Peer(pid=43, uid=os.geteuid(), gid=os.getegid()))
    stop = threading.Event()
    path = str(tmp_path / "pipeline.sock")
    code = "import json,sys; v=json.load(sys.stdin); assert 'lease_token' not in v; print(" + repr(result_text(case_id)) + ")"
    marker = tmp_path / "child.pid"
    if cancel:
        code = "import os,pathlib,time; pathlib.Path('child.pid').write_text(str(os.getpid())); time.sleep(30)"
    executor = process_executor(argv=[sys.executable, "-c", code], cwd=tmp_path,
                                env={"HOME": str(tmp_path)}, timeout=2, heartbeat_interval=0.05)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        listener.listen(4)
        def server():
            db = connect(config.database_path)
            try:
                return serve(db, listener, config=cfg, worker_uid=worker_uid,
                             control_key=b"t"*32, stop_event=stop)
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(server)
            try:
                def transport(value):
                    if cancel and marker.exists() and value["method"] == "renew":
                        conn.execute("UPDATE jobs SET state='cancelled' WHERE job_id='job-1'")
                    return request_at(path, value, control_uid=os.geteuid())
                if cancel:
                    with pytest.raises(ProtocolError):
                        run_one(claim_request_id=str(uuid4()), transport=transport, executor=executor)
                else:
                    result = run_one(claim_request_id=str(uuid4()), transport=transport, executor=executor)
            finally:
                stop.set()
                counts = future.result(timeout=5)
    if cancel:
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
    else:
        assert result == {"state": "report_received", "job_id": "job-1", "repair_verified": False}
    assert counts["connections"] >= (6 if cancel else 7)
    assert counts["transport_rejections"] == 0
    assert conn.execute("SELECT count(*) FROM broker_execution_starts").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM broker_results").fetchone()[0] == (0 if cancel else 1)
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == ("cancelled" if cancel else "running")
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
