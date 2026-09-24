"""Three bounded process pools: query, debug/push and synchronization.

The supervisor holds no job leases. Each child uses the existing claim/fence
path and owns its own SQLite connection and health identity.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time

from .cli import DEFAULT_CONFIG
from .config import load_config
from .db import connect, migrate
from .operations import heartbeat
from .services import JOB_POOLS, job_worker_main


def stop_process_groups(children, *, grace_seconds=5, signal_group=os.killpg):
    """Children are session leaders created by this supervisor, never arbitrary PIDs.

    Signal groups even if the direct child exited: its descendants may remain.
    This does not revoke board leases or claim successful device cleanup.
    """
    for process in children.values():
        try:
            signal_group(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    for process in children.values():
        try:
            signal_group(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for process in children.values():
        process.wait(timeout=5)


def supervise(
    config_path,
    *,
    popen=None,
    stopped=None,
    wait=time.sleep,
    shutdown=stop_process_groups,
):
    popen = popen or subprocess.Popen
    cfg = load_config(config_path)
    lock_path = cfg.database_path.resolve().with_suffix(".job-supervisor.lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        raise RuntimeError("job supervisor already running for this database") from None
    try:
        return _supervise(
            config_path, cfg, popen=popen, stopped=stopped, wait=wait, shutdown=shutdown
        )
    finally:
        os.close(lock_fd)


def _supervise(config_path, cfg, *, popen, stopped, wait, shutdown):
    conn = connect(cfg.database_path)
    migrate(conn)
    children = {}
    should_stop = stopped or (lambda: False)
    failed = {}
    try:
        for pool in JOB_POOLS:
            children[pool] = popen(
                [
                    sys.executable,
                    "-m",
                    "k3_support.worker_supervisor",
                    "--config",
                    str(config_path),
                    "--child",
                    pool,
                ],
                start_new_session=True,
            )
        while not should_stop():
            failed = {
                pool: process.poll()
                for pool, process in children.items()
                if process.poll() is not None
            }
            heartbeat(
                conn,
                "job_worker",
                "degraded" if failed else "ready",
                {
                    "heartbeat_phase": "supervising",
                    "worker_pools": list(JOB_POOLS),
                    "failed_children": failed,
                },
            )
            if failed:
                return 1
            wait(1)
        return 0
    finally:
        try:
            shutdown(children)
            for pool, process in children.items():
                if getattr(process, "pid", None) is not None:
                    conn.execute(
                        """UPDATE service_state SET status='stopped'
                                 WHERE component=? AND pid=?""",
                        ("job_worker:" + pool, process.pid),
                    )
            heartbeat(
                conn,
                "job_worker",
                "degraded" if failed else "stopped",
                {
                    "heartbeat_phase": "stopped",
                    "worker_pools": list(JOB_POOLS),
                    "failed_children": failed,
                },
            )
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--child", choices=tuple(JOB_POOLS))
    args = parser.parse_args()
    if args.child:
        sys.argv = [sys.argv[0], "--config", args.config]
        job_worker_main(args.child)
        return 0
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return supervise(args.config, stopped=lambda: stopping)


if __name__ == "__main__":
    raise SystemExit(main())
