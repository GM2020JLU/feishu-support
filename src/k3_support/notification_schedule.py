"""Daily work-hour boundaries; all persisted comparisons use UTC."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


def window(config, now):
    if now.tzinfo is None:
        raise ValueError("schedule time requires a timezone")
    observed = now.astimezone(UTC)
    zone = ZoneInfo(config.raw["timezone"])
    hours = config.work_hours
    start = time.fromisoformat(hours["start"])
    end = time.fromisoformat(hours["end"])
    today = observed.astimezone(zone).date()
    periods = []
    for offset in (-2, -1, 0, 1, 2):
        day = today + timedelta(days=offset)
        first = datetime.combine(day, start, tzinfo=zone).astimezone(UTC)
        last = datetime.combine(
            day + timedelta(days=end <= start), end, tzinfo=zone
        ).astimezone(UTC)
        periods.append((first, last))
    working = any(first <= observed < last for first, last in periods)
    return {
        "active": not working,
        "until_at": min(first for first, _ in periods if first > observed).isoformat()
        if not working
        else None,
        "cutoff": max(first for first, _ in periods if first <= observed).isoformat(),
    }
