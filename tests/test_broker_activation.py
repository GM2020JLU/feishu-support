import socket
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("variant", ["valid", "wrong_pid", "wrong_name", "no_fds", "connected"])
def test_native_activation_in_disposable_child(tmp_path, variant):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as inherited:
        inherited.bind(str(tmp_path / "activation.sock"))
        if variant != "connected":
            inherited.listen()
        script = r'''
import os, sys
from k3_support.broker_activation import take_listener
def guard(event, args):
    if event in {"socket.connect", "subprocess.Popen", "os.system", "os.exec"}:
        raise AssertionError("external action forbidden")
sys.addaudithook(guard)
source, variant = int(sys.argv[1]), sys.argv[2]
os.dup2(source, 3, inheritable=True)
os.environ.update(LISTEN_PID=str(os.getpid() if variant != "wrong_pid" else 1),
                  LISTEN_FDS="0" if variant == "no_fds" else "1",
                  LISTEN_FDNAMES="other" if variant == "wrong_name" else "broker")
try:
    listener = take_listener()
except ValueError:
    assert variant != "valid"
else:
    assert variant == "valid"
    assert not listener.get_inheritable()
    listener.close()
assert not any(key.startswith("LISTEN_") for key in os.environ)
print("activation verified")
'''
        result = subprocess.run(
            [sys.executable, "-c", script, str(inherited.fileno()), variant],
            pass_fds=(inherited.fileno(),), cwd=tmp_path,
            env={"HOME": str(tmp_path), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True, text=True, timeout=5, check=False,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "activation verified"
