"""Stop a synthetic transient user service, including a setsid descendant.

Never targets a pre-existing unit or the caller's cgroup. No production worker,
board, model, or account is used. RuntimeMaxSec provides an independent bound.
"""

import argparse
import json
import os
import select
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

CHILD = r'''
import json, os, sys, time
child = os.fork()
if child == 0:
    os.setsid()
    time.sleep(60)
else:
    with open(sys.argv[1], "w") as stream:
        json.dump({"parent": os.getpid(), "child": child}, stream)
    time.sleep(60)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heartbeat", action="store_true",
                        help="exercise real broker_process heartbeat failure instead of StopUnit")
    args = parser.parse_args()
    runtime = Path("/run/user") / str(os.geteuid())
    info = runtime.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077 or not stat.S_ISSOCK((runtime / "bus").stat().st_mode):
        raise RuntimeError("private user runtime and bus required")
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": str(runtime),
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(runtime / "bus")}
    unit = "codex-k3-stop-canary-" + uuid4().hex + ".service"

    def command(argv):
        return subprocess.run(argv, env=env, cwd="/tmp", capture_output=True,
                              text=True, timeout=10, check=True).stdout.strip()

    with tempfile.TemporaryDirectory(prefix="codex-cgroup-stop-") as directory:
        record = Path(directory) / "pids.json"
        revoked = Path(directory) / "revoked"
        observed = Path(directory) / "heartbeat-observed"
        program = CHILD
        if args.heartbeat:
            program = f'''
from pathlib import Path
from k3_support.broker_process import run_process
def heartbeat():
    if Path({str(revoked)!r}).exists():
        Path({str(observed)!r}).touch()
        raise ValueError("synthetic revoked authority")
try:
    run_process(argv=["/usr/bin/python3", "-c", {CHILD!r}, {str(record)!r}],
                cwd={directory!r}, env={{"PATH": "/usr/bin:/bin"}}, stdin=b"",
                heartbeat=heartbeat, timeout=15, heartbeat_interval=0.1)
except ValueError:
    pass
'''
        descriptors = []
        try:
            command(["systemd-run", "--user", "--quiet", "--collect", "--unit", unit,
                     "--property=Type=exec", "--property=KillMode=control-group",
                     "--property=RuntimeMaxSec=20", "--property=TimeoutStopSec=3",
                     "--property=Restart=no", os.path.abspath(sys.executable), "-c", program, str(record)])
            deadline = time.monotonic() + 5
            while True:
                try:
                    pids = json.loads(record.read_text())
                    break
                except (FileNotFoundError, json.JSONDecodeError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("synthetic service did not become ready") from None
                    time.sleep(0.05)
            group = command(["systemctl", "--user", "show", "--property=ControlGroup", "--value", unit])
            if not group.endswith("/" + unit):
                raise RuntimeError("unexpected service cgroup")
            for pid in pids.values():
                descriptors.append(os.pidfd_open(pid))
                if "0::" + group not in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
                    raise RuntimeError("synthetic process outside expected service")
            # Wait for the child to finish setsid, not merely for fork to return.
            while os.getsid(pids["child"]) != pids["child"]:
                if time.monotonic() >= deadline:
                    raise RuntimeError("descendant did not detach")
                time.sleep(0.01)
            if select.select(descriptors, [], [], 0)[0]:
                raise RuntimeError("synthetic process exited before stop test")
            if args.heartbeat:
                revoked.touch()
            else:
                command(["systemctl", "--user", "stop", unit])
            for descriptor in descriptors:
                if not select.select([descriptor], [], [], 3)[0]:
                    raise RuntimeError("service stop left a synthetic process alive")
            if args.heartbeat and not observed.exists():
                raise RuntimeError("heartbeat failure was not observed")
            print(json.dumps({"ok": True, "detached_descendant_exited": True,
                              "parent_exited": True, "pidfd_verified": True,
                              "real_broker_process_heartbeat": args.heartbeat,
                              "production_worker_integration_verified": False}))
        finally:
            # Exact task-owned random unit only; never signal the desktop cgroup.
            subprocess.run(["systemctl", "--user", "stop", unit], env=env, cwd="/tmp",
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            for descriptor in descriptors:
                os.close(descriptor)


if __name__ == "__main__":
    main()
