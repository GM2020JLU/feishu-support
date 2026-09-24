"""No-model Codex sandbox probe using only disposable local fixtures."""

import json
import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


CHILD = r'''
import errno, json, os, socket, sys
result = {"synthetic_environment_visible": os.environ.get("K3_SUPPORT_BROKER_TASK") == "synthetic-not-a-token"}
try:
    with open(sys.argv[2], "a") as f:
        f.write("unexpected")
    result["outside_write_denied"] = False
except OSError as e:
    if e.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
        raise
    result["outside_write_denied"] = True
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect(sys.argv[1])
        s.sendall(b"synthetic-canary")
    result["ipc_connected"] = True
except OSError as e:
    result["ipc_connected"] = False
    result["ipc_errno"] = e.errno
print(json.dumps(result))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", help="Direct Codex executable; avoid personal environment wrappers")
    parser.add_argument("--allow-broker-socket", action="store_true")
    args = parser.parse_args()
    codex = args.codex or shutil.which("codex")
    if not codex:
        raise SystemExit("codex unavailable")
    with tempfile.TemporaryDirectory(prefix="codex-ipc-canary-") as name:
        root = Path(name)
        work = root / "work"
        home = root / "home"
        work.mkdir()
        home.mkdir()
        fixture = root / "outside"
        fixture.write_text("fixture")
        path = str(root / "broker.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(1)
            server.settimeout(1)
            env = {"HOME": str(home), "CODEX_HOME": str(home), "PATH": "/usr/bin:/bin",
                   "LANG": "C.UTF-8", "K3_SUPPORT_BROKER_TASK": "synthetic-not-a-token"}
            extra = ["-c", 'permissions.canary.network.unix_sockets={' + json.dumps(path) + '="allow"}',
                     "-c", 'permissions.canary.network.enabled=true',
                     "-c", 'permissions.canary.network.mode="limited"',
                     "-c", 'permissions.canary.network.domains={}',
                     "-c", 'features.network_proxy.enabled=true',
                     "-c", 'permissions.canary.network.proxy_url="http://127.0.0.1:0"',
                     "-c", 'permissions.canary.network.socks_url="socks5://127.0.0.1:0"'] if args.allow_broker_socket else []
            result = subprocess.run([codex, "sandbox", "-P", "canary",
                                     "-c", 'permissions.canary.extends=":read-only"',
                                     "-c", 'permissions.canary.filesystem={' + json.dumps(str(work)) + '="write"}',
                                     "-c", 'permissions.canary.network.enabled=false',
                                     *extra,
                                     "-C", str(work), sys.executable, "-c", CHILD, path, str(fixture)],
                                    env=env, cwd=work, capture_output=True, text=True, timeout=20)
            if result.returncode:
                print(json.dumps({"ok": False, "exit_code": result.returncode, "diagnostic": result.stderr[-2000:]}))
                return 1
            report = json.loads(result.stdout)
            if report["ipc_connected"]:
                with server.accept()[0] as peer:
                    peer.settimeout(1)
                    report["payload_verified"] = peer.recv(64) == b"synthetic-canary"
            report["outside_unchanged"] = fixture.read_text() == "fixture"
            report["model_invoked"] = False
            report["socket_allowlist_requested"] = args.allow_broker_socket
            report["remote_tool_compatible"] = bool(report["ipc_connected"] and report.get("payload_verified"))
            print(json.dumps(report))
            return 0 if report["outside_write_denied"] and report["outside_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
