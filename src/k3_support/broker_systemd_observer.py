"""Control-side, read-only system service observation. No worker-provided status."""

import re
import subprocess
from uuid import UUID

from .broker_execution_instances import register
from .db import transaction
from .timeutil import iso_now

FIELDS = ("Id", "LoadState", "ActiveState", "SubState", "InvocationID", "ControlGroup", "MainPID")


def observe_running(conn, *, grant_id, claim_request_id):
    """Read the system manager directly and bind its running instance.

    This is not a worker RPC and never starts/stops a service. Deployment must
    isolate the control DB and prevent workers from controlling system units.
    """
    if not isinstance(claim_request_id, str) or str(UUID(claim_request_id)) != claim_request_id:
        raise ValueError("canonical claim identity required")
    unit = f"k3-support-broker-worker@{claim_request_id}.service"
    values = _observe(unit, FIELDS)
    if (values["ActiveState"] != "active" or values["SubState"] != "running"
            or not re.fullmatch(r"[1-9][0-9]{0,9}", values["MainPID"])):
        raise ValueError("exact running service instance required")
    return register(conn, grant_id=grant_id, claim_request_id=claim_request_id,
                    invocation_id=values["InvocationID"], cgroup_path=values["ControlGroup"])


def observe_instance(conn, *, grant_id, claim_request_id):
    """Bind a running instance or the manager's retained terminal invocation.

    A removed cgroup is reconstructed only for our explicitly fixed system.slice
    deployment. This records identity, never proof of descendant cleanup. Lost
    invocation/exit metadata remains unknown; there is no restart fallback.
    """
    if not isinstance(claim_request_id, str) or str(UUID(claim_request_id)) != claim_request_id:
        raise ValueError("canonical claim identity required")
    unit = f"k3-support-broker-worker@{claim_request_id}.service"
    values = _observe(unit, (*FIELDS, "ExecMainPID", "ExecMainCode", "ExecMainStatus",
                             "RemainAfterExit", "Slice"))
    if (values["ActiveState"], values["SubState"]) == ("active", "running"):
        if not re.fullmatch(r"[1-9][0-9]{0,9}", values["MainPID"]):
            raise ValueError("exact running service instance required")
        path = values["ControlGroup"]
    else:
        if values["RemainAfterExit"] != "yes" or values["Slice"] != "system.slice":
            raise ValueError("retained service invocation required")
        path = f"/system.slice/{unit}"
        _validate_exit(values, invocation_id=values["InvocationID"], cgroup_path=path)
    return register(conn, grant_id=grant_id, claim_request_id=claim_request_id,
                    invocation_id=values["InvocationID"], cgroup_path=path)


def _validate_exit(values, *, invocation_id, cgroup_path):
    if (values["InvocationID"] != invocation_id
            or (values["ActiveState"], values["SubState"]) not in {
                ("inactive", "dead"), ("failed", "failed"), ("active", "exited")}
            or values["MainPID"] != "0"
            or values["ControlGroup"] not in {"", cgroup_path}
            or not re.fullmatch(r"[1-9][0-9]{0,9}", values["ExecMainPID"])
            or values["ExecMainCode"] not in {"1", "2", "3"}
            or not re.fullmatch(r"[0-9]{1,3}", values["ExecMainStatus"])):
        raise ValueError("same service instance exit not verified")
    code, status = int(values["ExecMainCode"]), int(values["ExecMainStatus"])
    if status > 255 or (code != 1 and not 1 <= status <= 64):
        raise ValueError("invalid service exit status")
    return code, status


def _observe(unit, fields):
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "--system", "show", "--no-pager",
             "--property=" + ",".join(fields), unit],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
            check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise ValueError("service manager observation unavailable") from None
    if result.returncode or len(result.stdout) > 16384:
        raise ValueError("service manager observation unavailable")
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in fields or key in values:
            raise ValueError("invalid service manager observation")
        values[key] = value
    if set(values) != set(fields) or values["Id"] != unit or values["LoadState"] != "loaded":
        raise ValueError("exact loaded service required")
    return values


def observe_exit(conn, *, grant_id):
    """Persist exact service-main exit evidence, never descendant/repair proof."""
    instance = conn.execute("SELECT * FROM broker_execution_instances WHERE grant_id=?", (grant_id,)).fetchone()
    if instance is None:
        raise ValueError("previously observed execution instance required")
    values = _observe(instance["unit_name"], (*FIELDS, "ExecMainPID", "ExecMainCode", "ExecMainStatus"))
    code, status = _validate_exit(values, invocation_id=instance["invocation_id"],
                                  cgroup_path=instance["cgroup_path"])
    evidence = (grant_id, instance["invocation_id"], int(values["ExecMainPID"]), code, status)
    with transaction(conn):
        current = conn.execute("SELECT * FROM broker_execution_instances WHERE grant_id=?", (grant_id,)).fetchone()
        if current is None or tuple(current) != tuple(instance):
            raise ValueError("execution instance changed during observation")
        old = conn.execute("SELECT * FROM broker_service_exits WHERE grant_id=?", (grant_id,)).fetchone()
        if old and tuple(old)[:5] != evidence:
            raise ValueError("service exit evidence already recorded")
        if old is None:
            conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)", (*evidence, iso_now()))
    return {"service_main_exited": True, "descendant_isolation_verified": False,
            "board_cleanup_verified": False}
