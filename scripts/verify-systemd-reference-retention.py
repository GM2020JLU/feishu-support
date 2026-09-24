"""Native RefUnit canary against a task-owned transient user service only."""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from k3_support.broker_unit_references import UnitReferences


def main():
    runtime = f"/run/user/{os.geteuid()}"
    os.environ["XDG_RUNTIME_DIR"] = runtime
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + runtime + "/bus"
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": runtime,
                   "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + runtime + "/bus"}
    unit = "codex-k3-reference-canary-" + uuid4().hex + ".service"

    def command(*args):
        return subprocess.run(list(args), capture_output=True, text=True,
                              check=True, timeout=5, env=environment).stdout.strip()

    def observation():
        value = command("systemctl", "--user", "show", unit,
                        "--property=LoadState,ActiveState,InvocationID,ExecMainCode,ExecMainStatus")
        return dict(line.split("=", 1) for line in value.splitlines())

    references = UnitReferences(scope="user")
    with tempfile.TemporaryDirectory(prefix="codex-ref-retention-") as directory:
        marker = Path(directory) / "finish"
        program = "import pathlib,time,sys\np=pathlib.Path(sys.argv[1])\nwhile not p.exists(): time.sleep(0.02)"
        try:
            command("systemd-run", "--user", "--quiet", "--unit", unit,
                    "--property=RuntimeMaxSec=15", "--property=Restart=no",
                    "--property=Type=exec", "/usr/bin/python3", "-c", program, str(marker))
            references._call("RefUnit", unit)
            before = observation()
            if not before["InvocationID"] or before["ActiveState"] != "active":
                raise RuntimeError("running instance unavailable")
            marker.touch()
            deadline = time.monotonic() + 5
            while True:
                after = observation()
                if after["ActiveState"] == "inactive":
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("synthetic service did not exit")
                time.sleep(0.05)
            if (after["InvocationID"] != before["InvocationID"] or after["LoadState"] != "loaded"
                    or after["ExecMainCode"] != "1" or after["ExecMainStatus"] != "0"):
                raise RuntimeError("same-instance exit evidence not retained")
            references._call("UnrefUnit", unit)
            print(json.dumps({"ok": True, "native_ref_unref": True,
                              "same_invocation_exit_retained": True, "scope": "user",
                              "production_system_scope_verified": False}))
        finally:
            references.close()
            subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True,
                           timeout=5, check=False, env=environment)


if __name__ == "__main__":
    main()
