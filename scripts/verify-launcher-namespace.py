#!/usr/bin/env python3
"""Synthetic user-namespace launcher canary; never calls the service manager."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4


def child_namespace():
    from k3_support.broker_launcher import request, serve_connection

    assert os.geteuid() == 0
    with tempfile.TemporaryDirectory(prefix="codex-launcher-canary-") as temporary:
        root = Path(temporary)
        root.chmod(0o711)
        state = root / "state"
        state.mkdir(mode=0o700)
        descriptor = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        calls = []
        try:
            request_id = str(uuid4())
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                endpoint = root / "launcher.sock"
                listener.bind(str(endpoint))
                endpoint.chmod(0o666)  # Kernel UID check must stand on its own.
                listener.listen(1)
                listener.settimeout(5)
                for identity, expected in [(1, True), (1, False), (2, False)]:
                    pid = os.fork()
                    if pid == 0:
                        listener.close()
                        os.close(descriptor)
                        os.setgroups([])
                        os.setgid(identity)
                        os.setuid(identity)
                        try:
                            accepted = request(str(endpoint), request_id)
                        except (OSError, ValueError):
                            accepted = False
                        os._exit(0 if accepted is expected else 1)
                    reaped = False
                    try:
                        connection, _ = listener.accept()
                        try:
                            serve_connection(connection, control_uid=1, state_fd=descriptor,
                                             launch=lambda value: calls.append(value) or True)
                        except (OSError, ValueError):
                            assert not expected
                        _, status = os.waitpid(pid, 0)
                        reaped = True
                        assert os.waitstatus_to_exitcode(status) == 0
                    finally:
                        if not reaped:
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            os.waitpid(pid, 0)
            assert calls == [request_id]
            assert list(path.name for path in state.iterdir()) == [request_id]
        finally:
            os.close(descriptor)
    print(json.dumps({"kernel_peer_identity": True, "root_peer_checked_by_client": True,
                      "duplicate_denied": True, "wrong_uid_denied": True,
                      "launch_calls": 1, "host_root_service_verified": False,
                      "real_service_started": False}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--inside"]:
        child_namespace()
    else:
        result = subprocess.run(["unshare", "--user", "--map-auto", "--map-root-user",
                                 sys.executable, str(Path(__file__).resolve()), "--inside"],
                                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                                     "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
                                capture_output=True, text=True, timeout=20, check=False)
        if result.returncode:
            print("Namespace canary failed; no production action performed.", file=sys.stderr)
            raise SystemExit(1)
        print(result.stdout.strip())
