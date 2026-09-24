import hashlib
import json
import shlex
import subprocess
from uuid import uuid4

import pytest
from test_broker_remote_runner import remote  # noqa: F401

from k3_support.broker_remote_probe import command, observe
from k3_support.broker_remote_runner import run_one
from k3_support.remote_guard import receipt_directory
from k3_support.remote_journal import RemoteJournal


@pytest.fixture
def journal_remote(config, request):
    config.raw["runtime"]["remote_receipt_directory"] = "/var/lib/k3-receipts"
    return request.getfixturevalue("remote")


def test_queue_binds_journal_and_configuration_change_prevents_execution(conn, journal_remote):
    cfg, reader = journal_remote
    row = conn.execute("SELECT * FROM broker_remote_actions").fetchone()
    plan = json.loads(row["plan_json"])
    assert plan["guard_version"] == 2 and plan["receipt_directory"] == "/var/lib/k3-receipts"
    assert len(plan["command_digest"]) == 64 and row["request_id"] in plan["command"]
    cfg.raw["runtime"]["remote_receipt_directory"] = "/var/lib/k3-receipts-new"
    assert run_one(conn, cfg, contract_reader=reader,
                   transport=lambda **kw: pytest.fail("changed plan executed"))["state"] == "cancelled"


@pytest.mark.parametrize("fault", [None, "wrong_id", "boolean_code", "timeout", "duplicate", "extra", "missing"])
def test_receipt_probe_is_bound_readonly_and_never_authorizes_recovery(conn, journal_remote, fault):
    cfg, _ = journal_remote
    row = conn.execute("SELECT * FROM broker_remote_actions").fetchone()
    plan = json.loads(row["plan_json"])
    before = list(conn.iterdump())
    def transport(**kwargs):
        assert kwargs["argv"][:6] == [cfg.runtime("ssh_command"), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", cfg.runtime("remote_host")]
        if fault == "timeout":
            raise OSError("private failure")
        value = {"state": "guardian_returned", "version": 1, "request_id": row["request_id"],
                 "command_digest": plan["command_digest"], "guard_exit_code": 124}
        if fault == "wrong_id":
            value["request_id"] = str(uuid4())
        if fault == "boolean_code":
            value["guard_exit_code"] = True
        if fault == "extra":
            value["private"] = "secret"
        if fault == "missing":
            value = {"state": "unknown"}
        raw = json.dumps(value, sort_keys=True) + "\n"
        if fault == "duplicate":
            raw = raw.replace('{', '{"version":1,', 1)
        return {"exit_code": 0, "stdout": raw, "stderr": ""}
    result = observe(conn, cfg, request_id=row["request_id"], transport=transport)
    assert result["state"] == ("guardian_returned" if fault is None else "unknown")
    assert result["read_only"] and not result["recovery_authorized"]
    assert "secret" not in str(result) and list(conn.iterdump()) == before


def test_actual_embedded_query_reads_receipt_without_reexecuting(tmp_path):
    tmp_path.chmod(0o700)
    request = str(uuid4())
    journal = RemoteJournal(str(tmp_path), request, "not a runnable command")
    journal.finish(7)
    journal.close()
    digest = hashlib.sha256(b"not a runnable command").hexdigest()
    result = subprocess.run(shlex.split(command(str(tmp_path), request, digest)),
                            capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0 and json.loads(result.stdout)["guard_exit_code"] == 7
    assert len(list(tmp_path.iterdir())) == 2


@pytest.mark.parametrize("path", ["/", "/usr/private", "/tmp/receipt", "/srv/project/receipts",
                                "/srv", "/opt/compiler/receipts", "/private/../receipt", "/private//receipt"])
def test_receipt_directory_cannot_be_exposed_to_worker(path):
    with pytest.raises(ValueError):
        receipt_directory({"remote_receipt_directory": path, "remote_workspace_root": "/srv/project",
                           "remote_toolchain_roots": ["/opt/compiler"]})
