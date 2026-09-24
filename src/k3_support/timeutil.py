from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_observation: ContextVar[datetime | None] = ContextVar('support_clock_observation', default=None)


@contextmanager
def observed_clock(at: datetime):
    """Context-local business time for deterministic replay, not lease timers.

    Wall/monotonic transport deadlines are intentionally not altered. Callers
    must not assume legacy direct datetime.now users are covered by this clock.
    """
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('clock observation requires a timezone-aware datetime')
    token = _observation.set(at.astimezone(UTC))
    try:
        yield
    finally:
        _observation.reset(token)


def utc_now() -> datetime:
    return _observation.get() or datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat(timespec="milliseconds")


def epoch_now() -> int:
    return int(utc_now().timestamp())


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)
