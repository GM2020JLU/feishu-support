"""Standalone remote heartbeat guardian. This file is transported as trusted code."""

import os
import selectors
import signal
import subprocess
import sys
import time


def supervise(command):
    process = None
    expires = 0
    monotonic_expiry = 0
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    buffer = b""
    started = time.monotonic()
    with selectors.DefaultSelector() as selector:
        selector.register(sys.stdin.buffer, selectors.EVENT_READ)
        try:
            while not stopping and time.monotonic()-started < 7200:
                for _, _ in selector.select(.1):
                    data = os.read(sys.stdin.fileno(), 4096)
                    if not data:
                        return 124
                    buffer += data
                    if len(buffer) > 4096:
                        return 124
                    while b"\n" in buffer:
                        packet, buffer = buffer.split(b"\n", 1)
                        if len(packet) != 13 or not packet.isdigit():
                            return 124
                        candidate = int(packet)
                        now = int(time.time()*1000)
                        if not now < candidate <= now+6000:
                            return 124
                        expires = candidate
                        monotonic_expiry = time.monotonic()+min(5, (candidate-now)/1000)
                if process is None:
                    if not expires:
                        if time.monotonic()-started > 5:
                            return 124
                        continue
                    # The trusted command execs bwrap with a dedicated PID namespace.
                    process = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL,
                                               start_new_session=True, close_fds=True)
                if time.monotonic() >= monotonic_expiry or int(time.time()*1000) >= expires:
                    return 124
                exited = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None:
                    return exited.si_status if exited.si_code == os.CLD_EXITED else 125
            return 124
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)


if __name__ == "__main__":
    journal = None
    try:
        if len(sys.argv) == 4:
            journal = globals()["RemoteJournal"](sys.argv[2], sys.argv[3], sys.argv[1])
        elif len(sys.argv) != 2:
            raise ValueError("invalid guardian arguments")
        code = supervise(sys.argv[1])
        if journal is not None:
            journal.finish(code)
        raise SystemExit(code)
    finally:
        if journal is not None:
            journal.close()
