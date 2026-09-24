"""Bounded Linux child supervision, without control DB access or inherited secrets."""

import math
import os
import selectors
import signal
import subprocess
import time


def run_process(*, argv, cwd, env, stdin, heartbeat, timeout=120, heartbeat_interval=10, output_limit=200000, detailed=False, keepalive=False, pass_fds=()):
    """Trusted adapter supplies argv/cwd/env; input never becomes shell or argv.

    Kills the owned process group on every exit, including same-group descendants.
    Escaped sessions require a deployment cgroup; this is not a sandbox.
    """
    if (not isinstance(argv, list) or not argv or not argv[0]
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)):
        raise ValueError("explicit argv required")
    if not isinstance(env, dict) or not isinstance(stdin, bytes) or len(stdin) > 262144:
        raise ValueError("bounded stdin and explicit environment required")
    if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
           for value in (timeout, heartbeat_interval)) or heartbeat_interval > timeout:
        raise ValueError("invalid execution deadlines")
    if type(output_limit) is not int or not 1 <= output_limit <= 2097152:
        raise ValueError("invalid output limit")
    if type(keepalive) is not bool or (keepalive and (stdin or heartbeat_interval > 2)):
        raise ValueError("invalid remote heartbeat stream")
    if (not isinstance(pass_fds, tuple) or len(pass_fds) > 4
            or any(type(fd) is not int or fd < 3 for fd in pass_fds)
            or len(set(pass_fds)) != len(pass_fds)):
        raise ValueError("invalid explicit descriptor allowlist")
    heartbeat()
    process = subprocess.Popen(argv, cwd=cwd, env=env, **({'pass_fds': pass_fds} if pass_fds else {}), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, close_fds=True)
    started = time.monotonic()
    def pulse():
        return (str(int(time.time()*1000)+5000)+"\n").encode()
    if keepalive:
        stdin = pulse()
    next_heartbeat = started + heartbeat_interval
    stdout = bytearray()
    stderr = bytearray()
    output_bytes = 0
    offset = 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream, event, label in ((process.stdin, selectors.EVENT_WRITE, "input"),
                                         (process.stdout, selectors.EVENT_READ, "output"),
                                         (process.stderr, selectors.EVENT_READ, "error")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event, label)
            while True:
                now = time.monotonic()
                if now - started >= timeout:
                    raise TimeoutError("worker execution deadline exceeded")
                if now >= next_heartbeat:
                    heartbeat()
                    if keepalive:
                        packet = pulse()
                        if os.write(process.stdin.fileno(), packet) != len(packet):
                            raise ValueError("remote heartbeat incomplete")
                    next_heartbeat = time.monotonic() + heartbeat_interval
                for key, _ in selector.select(min(0.1, max(0, next_heartbeat - now), timeout - (now-started))):
                    stream = key.fileobj
                    if key.data == "input":
                        try:
                            offset += os.write(stream.fileno(), stdin[offset:offset+16384])
                        except BrokenPipeError:
                            offset = len(stdin)
                        if offset == len(stdin):
                            selector.unregister(stream)
                            if not keepalive:
                                stream.close()
                    else:
                        chunk = os.read(stream.fileno(), 16384)
                        if not chunk:
                            selector.unregister(stream)
                            stream.close()
                            continue
                        output_bytes += len(chunk)
                        if output_bytes > output_limit:
                            raise ValueError("worker output limit exceeded")
                        if key.data == "output":
                            stdout.extend(chunk)
                        elif detailed:
                            stderr.extend(chunk)
                # Observe without reaping: keep PID reserved until group cleanup.
                exited = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None and not selector.get_map():
                    break
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            process.wait(timeout=5)
    if detailed:
        return {"exit_code": process.returncode, "stdout": stdout.decode("utf-8"), "stderr": stderr.decode("utf-8")}
    if process.returncode != 0:
        raise ValueError("worker command failed")
    return stdout.decode("utf-8")
