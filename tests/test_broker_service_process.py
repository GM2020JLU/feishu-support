import json
import socket
import subprocess
import sys
from pathlib import Path

import yaml


def test_native_service_activation_and_sigterm_preserves_database(conn, config, tmp_path):
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    private = tmp_path / "keys"
    private.mkdir(mode=0o700)
    key = private / "broker.key"
    key.write_bytes(b"t" * 32)
    key.chmod(0o600)
    contract = private / "execution-contract.json"
    contract.write_text(json.dumps({"version": 1, "provider": "synthetic",
                                   "base_url": "https://example.com/v1", "model": "gpt-5.6-sol",
                                   "reasoning": "medium", "wire_api": "responses"}))
    contract.chmod(0o600)
    before = list(conn.iterdump())
    script = r'''
import os, signal, sys, threading
import k3_support.broker_service as service
def guard(event, args):
    if event in {"socket.connect", "subprocess.Popen", "os.system", "os.exec"}:
        raise AssertionError("external action forbidden")
sys.addaudithook(guard)
source = int(sys.argv[1])
os.dup2(source, 3, inheritable=True)
if source != 3:
    os.close(source)
os.environ.update(LISTEN_PID=str(os.getpid()), LISTEN_FDS="1", LISTEN_FDNAMES="broker")
original_serve = service.serve
timers = []
def observed_serve(*args, **kwargs):
    # Trigger only after configuration, key, schema and native activation succeed.
    assert kwargs["contract_reader"]().provider == "synthetic"
    timer = threading.Timer(0.02, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timers.append(timer)
    timer.start()
    return original_serve(*args, **kwargs)
service.serve = observed_serve
previous = signal.getsignal(signal.SIGTERM)
try:
    result = service.main(["--config", sys.argv[2], "--key-directory", sys.argv[3],
                           "--worker-uid", str(os.geteuid()+1), "--contract-directory", sys.argv[3]])
finally:
    for timer in timers:
        timer.cancel()
        timer.join(timeout=1)
assert result == 0 and len(timers) == 1
assert signal.getsignal(signal.SIGTERM) == previous
assert not any(key.startswith("LISTEN_") for key in os.environ)
try:
    os.fstat(3)
except OSError:
    pass
else:
    raise AssertionError("activated listener leaked")
print("service startup and SIGTERM verified")
'''
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(tmp_path / "broker.sock"))
        listener.listen()
        result = subprocess.run(
            [sys.executable, "-c", script, str(listener.fileno()), str(config.path), str(private)],
            pass_fds=(listener.fileno(),), cwd=tmp_path,
            env={"HOME": str(tmp_path), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True, text=True, timeout=5, check=False,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "service startup and SIGTERM verified"
    assert result.stderr == ""
    assert list(conn.iterdump()) == before
