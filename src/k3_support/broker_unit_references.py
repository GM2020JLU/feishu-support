"""Keep systemd unit evidence loaded via connection-scoped RefUnit calls.

References neither start services nor keep processes alive. Losing this broker
connection releases them; it is not persistent evidence across a broker crash.
"""

import ctypes
from uuid import UUID

from .broker_systemd_observer import observe_running


class UnitReferences:
    def __init__(self, *, scope="system"):
        if scope not in {"system", "user"}:
            raise ValueError("explicit systemd scope required")
        self.bus = ctypes.c_void_p()
        self.units = {}
        self.library = ctypes.CDLL("libsystemd.so.0")
        open_bus = getattr(self.library, "sd_bus_open_" + scope)
        open_bus.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        open_bus.restype = ctypes.c_int
        self.library.sd_bus_set_method_call_timeout.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self.library.sd_bus_set_method_call_timeout.restype = ctypes.c_int
        self.library.sd_bus_call_method.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                                                    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                                                    ctypes.c_void_p, ctypes.c_char_p]
        self.library.sd_bus_call_method.restype = ctypes.c_int
        self.library.sd_bus_close_unref.argtypes = [ctypes.c_void_p]
        self.library.sd_bus_close_unref.restype = ctypes.c_void_p
        if open_bus(ctypes.byref(self.bus)) < 0:
            raise ValueError("system manager reference connection unavailable")
        if self.library.sd_bus_set_method_call_timeout(self.bus, 2_000_000) < 0:
            self.close()
            raise ValueError("system manager reference deadline unavailable")

    def _call(self, method, unit):
        if not self.bus.value:
            raise ValueError("reference connection closed")
        result = self.library.sd_bus_call_method(
            self.bus, b"org.freedesktop.systemd1", b"/org/freedesktop/systemd1",
            b"org.freedesktop.systemd1.Manager", method.encode(), None, None, b"s",
            ctypes.c_char_p(unit.encode()),
        )
        if result < 0:
            raise ValueError("system manager reference operation unavailable")

    def observe(self, conn, *, grant_id, claim_request_id):
        if not isinstance(claim_request_id, str) or str(UUID(claim_request_id)) != claim_request_id:
            raise ValueError("canonical claim required")
        unit = f"k3-support-broker-worker@{claim_request_id}.service"
        if unit in self.units:
            raise ValueError("unit already retained")
        if len(self.units) >= 256:
            raise ValueError("reference capacity exhausted; reconcile executions")
        self._call("RefUnit", unit)
        self.units[unit] = grant_id
        # Keep the reference even if observation fails: start was already
        # committed and requires reconciliation, not an unobserved retry.
        return observe_running(conn, grant_id=grant_id, claim_request_id=claim_request_id)

    def release_recorded(self, conn):
        released = 0
        for unit, grant_id in list(self.units.items()):
            if conn.execute("SELECT 1 FROM broker_service_exits WHERE grant_id=?", (grant_id,)).fetchone():
                self._call("UnrefUnit", unit)
                del self.units[unit]
                released += 1
                if released == 2:
                    break

    def close(self):
        if self.bus.value:
            self.library.sd_bus_close_unref(self.bus)
            self.bus = ctypes.c_void_p()
        self.units.clear()


class RetainedUnitObservations:
    """Observe fixed RemainAfterExit units without privileged RefUnit calls.

    The root-owned worker template retains successful exits; failed units remain
    loaded until explicit reset. Missing/cleared metadata remains unknown and
    never permits a retry. UnitReferences is retained for legacy deployments.
    """

    def observe(self, conn, *, grant_id, claim_request_id):
        return observe_running(conn, grant_id=grant_id, claim_request_id=claim_request_id)

    def release_recorded(self, conn):
        return 0

    def close(self):
        pass
