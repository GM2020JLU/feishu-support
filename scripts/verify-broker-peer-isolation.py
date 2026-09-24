"""Real subordinate-UID canary; synthetic files only, no installed accounts.

Run with the installed project interpreter. Requires Linux unshare/newuidmap and
configured subordinate UID/GID ranges. This is not a production ACL/cgroup audit.
"""

import json
import os
import pwd
import shutil
import socket
import subprocess
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

from k3_support.broker_connection import serve_connection
from k3_support.broker_grants import issue, revoke
from k3_support.broker_identity import IdentityError, authenticate_worker
from k3_support.db import connect, migrate, transaction
from k3_support.execution_stop import apply, preview
from k3_support.ids import canonical_json, digest
from k3_support.store import create_case

CHILD = r'''
import json, os, socket, struct, sys, uuid
root = sys.argv[1]
result = {"namespace_uid": os.geteuid()}
for action, mode in (("read", "rb"), ("write", "ab")):
    try:
        with open(root + "/control/fixture", mode):
            pass
    except PermissionError:
        result[action + "_denied"] = True
    else:
        result[action + "_denied"] = False
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
    peer.settimeout(5)
    peer.connect(root + "/broker.sock")
    peer.sendall(json.dumps(result).encode() + b"\n")
    stream = peer.makefile("rb")
    binding = json.loads(stream.readline(4096))
    stream.close()
for stage in ("valid", "forged", "cross_case", "cross_job", "cancelled", "revoked"):
    params = dict(binding)
    if stage == "forged":
        params["lease_token"] = "forged-canary-token"
    if stage == "cross_case":
        params["case_id"] = "other-case"
    if stage == "cross_job":
        params["job_id"] = "other-job"
    method = "renew" if stage in ("cross_case", "cross_job", "cancelled") else "input"
    request = {"version": 1, "request_id": str(uuid.uuid4()), "method": method, "params": params}
    raw = json.dumps(request).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(5)
        peer.connect(root + "/broker.sock")
        peer.sendall(struct.pack("!I", len(raw)) + raw)
        stream = peer.makefile("rb")
        size = struct.unpack("!I", stream.read(4))[0]
        if not 0 < size <= 262144:
            raise RuntimeError("invalid frame")
        response = json.loads(stream.read(size))
        stream.close()
    if response["request_id"] != request["request_id"]:
        raise RuntimeError("response identity mismatch")
    if stage == "valid":
        if not response["ok"] or response["result"]["brief"] != "synthetic canary input":
            raise RuntimeError("authorized input not delivered")
        if "context_extra" in response["result"]:
            raise RuntimeError("private context leaked")
    elif response != {"version": 1, "request_id": request["request_id"],
                       "ok": False, "error": "stale_binding"}:
        raise RuntimeError("invalid grant not rejected")
'''


def seed(conn, worker_uid):
    migrate(conn)
    case, _ = create_case(conn, title="synthetic", case_type="bug", severity="P3", confidence=0.8)
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (case,))
    payload = {"case_id": case, "lifecycle_round": 1, "brief": "synthetic canary input",
               "repos": ["u-boot"], "model": "gpt-5.6-sol", "reasoning": "medium",
               "context_extra": {"private": "synthetic-only"}}
    fingerprint = digest(payload)
    now = datetime.now(UTC)
    conn.execute("""INSERT INTO jobs(job_id,case_id,job_type,state,lease_owner,lease_expires_at,
                    input_digest,attempt_no,available_at,created_at,updated_at)
                    VALUES('canary-job',?,'codex','running','canary',?,?,1,?,?,?)""",
                 (case, (now + timedelta(minutes=5)).isoformat(), fingerprint,
                  now.isoformat(), now.isoformat(), now.isoformat()))
    conn.execute("INSERT INTO broker_inputs VALUES(?,?,?)",
                 ("canary-job", canonical_json(payload), now.isoformat()))
    with transaction(conn):
        grant = issue(conn, job_id="canary-job", attempt_no=1, lease_owner="canary",
                      worker_uid=worker_uid, now=now)
    return {"job_id": "canary-job", "case_id": case, "execution_round": 1,
            "lifecycle_round": 1, "input_digest": fingerprint,
            "lease_token": grant["token"]}, grant["grant_id"]


def subordinate(path):
    user = pwd.getpwuid(os.geteuid()).pw_name
    for line in Path(path).read_text().splitlines():
        fields = line.split(":")
        if len(fields) == 3 and fields[0] in (user, str(os.geteuid())):
            start, count = map(int, fields[1:])
            if start > 0 and count >= 2:
                return start, count
    raise RuntimeError("subordinate identity range required")


def main():
    if os.geteuid() == 0:
        raise RuntimeError("run as the unprivileged control user")
    uid, uid_count = subordinate("/etc/subuid")
    gid, gid_count = subordinate("/etc/subgid")
    unshare = shutil.which("unshare")
    python = "/usr/bin/python3"
    if not unshare or not Path(python).is_file():
        raise RuntimeError("unshare and system Python required")
    with tempfile.TemporaryDirectory(prefix="codex-broker-peer-") as directory, ExitStack() as stack:
        root = Path(directory)
        # Only the synthetic socket is accessible. No production data lives here.
        root.chmod(0o711)
        control = root / "control"
        control.mkdir(mode=0o700)
        fixture = control / "fixture"
        fixture.write_bytes(b"synthetic-control-data")
        fixture.chmod(0o600)
        conn = connect(control / "state.sqlite")
        stack.callback(conn.close)
        binding, grant_id = seed(conn, uid + 1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(root / "broker.sock"))
            (root / "broker.sock").chmod(0o666)
            listener.listen(1)
            listener.settimeout(10)
            command = [unshare, "--user", "--map-users", f"0:{uid}:{uid_count}",
                       "--map-groups", f"0:{gid}:{gid_count}", "--setuid", "1",
                       "--setgid", "1", python, "-", directory]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, cwd="/tmp",
                                       env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
            try:
                process.stdin.write(CHILD.encode())
                process.stdin.close()
                process.stdin = None
                peer, _ = listener.accept()
                with peer:
                    peer.settimeout(5)
                    identity = authenticate_worker(peer, worker_uid=uid + 1)
                    try:
                        authenticate_worker(peer, worker_uid=uid + 2)
                    except IdentityError:
                        wrong_uid_denied = True
                    else:
                        wrong_uid_denied = False
                    data = bytearray()
                    while not data.endswith(b"\n") and len(data) < 4096:
                        chunk = peer.recv(4096 - len(data))
                        if not chunk:
                            break
                        data.extend(chunk)
                    result = json.loads(data)
                    peer.sendall(json.dumps(binding).encode() + b"\n")
                for stage in ("valid", "forged", "cross_case", "cross_job", "cancelled", "revoked"):
                    if stage == "cancelled":
                        from uuid import uuid4

                        current = preview(conn, job_id="canary-job")
                        apply(conn, job_id="canary-job", binding_digest=current["binding_digest"],
                              request_id=str(uuid4()), actor_id="synthetic-controller")
                    if stage == "revoked":
                        revoke(conn, grant_id=grant_id)
                    peer, _ = listener.accept()
                    before = list(conn.iterdump())
                    serve_connection(conn, peer, worker_uid=uid + 1)
                    if list(conn.iterdump()) != before:
                        raise RuntimeError("input or rejected request mutated control state")
                process.communicate(timeout=5)
                if process.returncode:
                    raise RuntimeError("namespace child failed")
                if result != {"namespace_uid": 1, "read_denied": True, "write_denied": True}:
                    raise RuntimeError("synthetic control access was not denied")
                if not wrong_uid_denied or identity.uid == os.geteuid():
                    raise RuntimeError("peer identity isolation failed")
                if fixture.read_bytes() != b"synthetic-control-data":
                    raise RuntimeError("synthetic control data changed")
                if conn.execute("SELECT count(*) FROM outbox").fetchone()[0]:
                    raise RuntimeError("unexpected outbound work")
                if conn.execute("SELECT state FROM jobs WHERE job_id='canary-job'").fetchone()[0] != "cancelled":
                    raise RuntimeError("operator stop did not cancel execution")
                if conn.execute("SELECT count(*) FROM execution_stop_requests").fetchone()[0] != 1:
                    raise RuntimeError("operator stop audit missing")
                print(json.dumps({"ok": True, "real_kernel_peer_uid": identity.uid,
                                  "control_uid": os.geteuid(), "wrong_uid_denied": True,
                                  "synthetic_control_read_write_denied": True,
                                  "authorized_input_delivered": True,
                                  "forged_and_revoked_grants_denied": True,
                                  "cross_case_and_job_renewal_denied": True,
                                  "operator_stop_blocks_renewal": True,
                                  "production_acl_verified": False,
                                  "cgroup_verified": False}))
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    main()
