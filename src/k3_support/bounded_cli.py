"""Bounded binary subprocess capture. No truncated output is a valid result."""

import os
import selectors
import signal
import subprocess
import time

CHUNK_BYTES = 4096
STDOUT_LIMIT = 8 * 1024 * 1024
STDERR_LIMIT = 512 * 1024


class OutputLimitError(RuntimeError):
    def __init__(self, stream):
        super().__init__(f'CLI {stream} exceeded its output budget; remote result unknown')
        self.stream = stream


def signal_owned_process(process, sig):
    """Only for a child this module/caller started in its own session."""
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def run(argv, *, timeout, env, stdout_limit=STDOUT_LIMIT, stderr_limit=STDERR_LIMIT):
    if timeout <= 0 or min(stdout_limit, stderr_limit) < 1:
        raise ValueError('positive subprocess time and output budgets required')
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, text=False, bufsize=0,
                               start_new_session=True)
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    limits = {'stdout': stdout_limit, 'stderr': stderr_limit}
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    try:
        for name in buffers:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fd, CHUNK_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if len(buffers[key.data]) + len(chunk) > limits[key.data]:
                    raise OutputLimitError(key.data)
                buffers[key.data].extend(chunk)
        process.wait(timeout=max(0.001, deadline-time.monotonic()))
        return subprocess.CompletedProcess(argv, process.returncode,
            bytes(buffers['stdout']).decode('utf-8'),
            bytes(buffers['stderr']).decode('utf-8', errors='replace'))
    finally:
        selector.close()
        # Closing output pipes alone cannot stop a child or its descendants.
        signal_owned_process(process, signal.SIGTERM)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            signal_owned_process(process, signal.SIGKILL)
            process.wait(timeout=1)
        finally:
            # A descendant may still own inherited pipes after its parent exits.
            signal_owned_process(process, signal.SIGKILL)
            for stream in (process.stdout, process.stderr):
                stream.close()
