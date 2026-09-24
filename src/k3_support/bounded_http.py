"""Loopback HTTP concurrency with admission before thread creation."""

import socket
import threading
from http.server import HTTPServer


class BoundedHTTPServer(HTTPServer):
    def __init__(self, address, handler, *, max_workers=8):
        if type(max_workers) is not int or not 1 <= max_workers <= 64:
            raise ValueError('HTTP worker limit must be between 1 and 64')
        self._slots = threading.BoundedSemaphore(max_workers)
        self._lock = threading.Lock()
        self._connections = set()
        self.active_workers = 0
        self.peak_workers = 0
        self.rejected_connections = 0
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self.rejected_connections += 1
            self.shutdown_request(request)
            return
        with self._lock:
            self._connections.add(request)
            self.active_workers += 1
            self.peak_workers = max(self.peak_workers, self.active_workers)
        thread = threading.Thread(target=self._handle_owned, args=(request, client_address),
                                  daemon=True, name='feishu-gui-http')
        try:
            thread.start()
        except Exception:
            self._release(request)

    def _release(self, request):
        try:
            self.shutdown_request(request)
        finally:
            with self._lock:
                self._connections.discard(request)
                self.active_workers -= 1
            self._slots.release()

    def _handle_owned(self, request, address):
        try:
            self.finish_request(request, address)
        except Exception:
            # Handler owns structured errors. Never dump cookies or bodies from
            # an unexpected exception into a system journal.
            pass
        finally:
            self._release(request)

    def server_close(self):
        super().server_close()
        with self._lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
