"""Streaming diagnostic line/ring budgets, including truncation suffixes."""

import re
import threading
from collections import deque

LINE_BYTES = 4096
HISTORY_BYTES = 512 * 1024
HISTORY_LINES = 200
SUFFIX = ' ... [truncated]'


def clipped(text, budget):
    return text.encode('utf-8')[:budget].decode('utf-8', errors='ignore')


class Diagnostics:
    def __init__(self, event_key):
        self.event_key = event_key
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.history = deque()
        self.line = bytearray()
        self.line_received = 0
        self.counters = dict(received_bytes=0, long_line_dropped_bytes=0, truncated_lines=0,
                             history_evicted_bytes=0, history_evicted_lines=0, history_bytes=0,
                             lines_seen=0, peak_line_bytes=0)

    @property
    def warnings(self):
        with self.lock:
            return list(self.history)

    @property
    def stats(self):
        with self.lock:
            return {**self.counters, 'partial_line_bytes': len(self.line), 'history_lines': len(self.history)}

    def _finish_line(self, *, complete):
        self.counters['lines_seen'] += 1
        truncated = self.line_received > LINE_BYTES
        decoded = bytes(self.line).decode('utf-8', errors='replace').rstrip('\r')
        if truncated:
            text = clipped(decoded, LINE_BYTES-len(SUFFIX.encode())) + SUFFIX
            kept = len(clipped(decoded, LINE_BYTES-len(SUFFIX.encode())).encode())
            self.counters['long_line_dropped_bytes'] += self.line_received-kept
            self.counters['truncated_lines'] += 1
        else:
            text = clipped(decoded, LINE_BYTES)
        marker = re.fullmatch(r'\[event\] ready event_key=([^\s]+)(?:\s+[^\r\n]*)?', text)
        if complete and not truncated and marker and marker.group(1) == self.event_key:
            self.ready.set()
        elif text.strip():
            size = len(text.encode())
            while self.history and (len(self.history) >= HISTORY_LINES or self.counters['history_bytes']+size > HISTORY_BYTES):
                evicted = self.history.popleft()
                count = len(evicted.encode())
                self.counters['history_bytes'] -= count
                self.counters['history_evicted_bytes'] += count
                self.counters['history_evicted_lines'] += 1
            self.history.append(text)
            self.counters['history_bytes'] += size
        self.line.clear()
        self.line_received = 0

    def feed(self, chunk):
        if not isinstance(chunk, bytes) or len(chunk) > LINE_BYTES:
            raise ValueError('diagnostics requires bounded binary chunks')
        with self.lock:
            self.counters['received_bytes'] += len(chunk)
            # Splitting only a bounded 4 KiB chunk cannot materialize a huge line.
            parts = chunk.split(b'\n')
            for index, part in enumerate(parts):
                self.line_received += len(part)
                self.line.extend(part[:max(0, LINE_BYTES-len(self.line))])
                self.counters['peak_line_bytes'] = max(self.counters['peak_line_bytes'], len(self.line))
                if index < len(parts)-1:
                    self._finish_line(complete=True)

    def finish(self):
        with self.lock:
            if self.line_received:
                self._finish_line(complete=False)
