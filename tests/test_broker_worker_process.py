import os
import sys
from uuid import uuid4

import pytest
from test_broker_input import seeded
from test_broker_results import NOW
from test_review import active_config, result_text

from k3_support.broker_input import read
from k3_support.broker_renew import renew
from k3_support.broker_results import submit
from k3_support.broker_start import authorize
from k3_support.broker_worker import process_executor, run_one


@pytest.mark.parametrize("cancel", [False, True])
def test_worker_real_process_submission_or_cancellation(conn, config, tmp_path, cancel):
    from test_broker_start import setup
    task = setup(conn, config)["params"]
    cfg = active_config(config)
    marker = tmp_path / "child.pid"
    methods = []
    def transport(request):
        method = request["method"]
        methods.append(method)
        if method == "claim":
            result = {"task": task}
        elif method == "start":
            result = authorize(conn, cfg, request, peer_uid=1234, now=NOW)
        elif method == "renew":
            if cancel and marker.exists():
                conn.execute("UPDATE jobs SET state='cancelled'")
            result = renew(conn, request, peer_uid=1234, now=NOW, config=cfg)
        else:
            result = {"input": read, "result": submit}[method](conn, request, peer_uid=1234, now=NOW)
        return {"version": 1, "request_id": request["request_id"], "ok": True, "result": result}
    code = """import json,os,pathlib,sys,time
data=json.load(sys.stdin)
assert set(data)=={'job_id','input_digest','brief','repos','model','reasoning'}
assert 'broker-secret' not in str(data)
pathlib.Path('child.pid').write_text(str(os.getpid()))
"""
    code += "time.sleep(30)" if cancel else "print(" + repr(result_text(task["case_id"])) + ")"
    executor = process_executor(argv=[sys.executable, "-c", code], cwd=tmp_path,
                                env={"HOME": str(tmp_path)}, timeout=2, heartbeat_interval=0.02)
    if cancel:
        with pytest.raises(ValueError):
            run_one(claim_request_id=str(uuid4()), transport=transport, executor=executor)
        assert "result" not in methods
        assert conn.execute("SELECT count(*) FROM broker_results").fetchone()[0] == 0
    else:
        outcome = run_one(claim_request_id=str(uuid4()), transport=transport, executor=executor)
        assert outcome["state"] == "report_received" and outcome["repair_verified"] is False
        assert conn.execute("SELECT count(*) FROM broker_results").fetchone()[0] == 1
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
