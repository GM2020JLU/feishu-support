import subprocess
import sys

import pytest

from k3_support.codex_remote import RemoteSandboxError, _run_supervised


@pytest.mark.parametrize("reason", ["revoked", "deadline", "success", "failure"])
def test_local_ssh_supervisor_reaps_owned_process(conn, monkeypatch, reason):
    processes = []
    native = subprocess.Popen
    def spawn(*args, **kwargs):
        child = native(*args, **kwargs)
        processes.append(child)
        return child
    monkeypatch.setattr("k3_support.codex_remote.subprocess.Popen", spawn)
    checks = []
    def heartbeat():
        checks.append(True)
        if reason == "revoked" and len(checks) > 1:
            raise RemoteSandboxError("synthetic revocation")
    command = "import time; time.sleep(30)" if reason in {"revoked", "deadline"} else f"raise SystemExit({int(reason == 'failure')})"
    if reason in {"revoked", "deadline"}:
        with pytest.raises(RemoteSandboxError):
            _run_supervised([sys.executable, "-c", command], heartbeat=heartbeat, timeout=0.05)
    else:
        assert _run_supervised([sys.executable, "-c", command], heartbeat=heartbeat) == int(reason == "failure")
    assert len(processes) == 1 and processes[0].returncode is not None
