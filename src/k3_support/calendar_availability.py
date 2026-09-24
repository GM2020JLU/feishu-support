"""Bounded Calendar v4 free/busy evidence; missing access is never free time."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from subprocess import TimeoutExpired
from zoneinfo import ZoneInfo

from .ids import canonical_json
from .timeutil import iso_now, parse_iso


def work_window(config, start):
    zone = ZoneInfo(config.raw["timezone"])
    local = parse_iso(start).astimezone(zone)
    hours = config.work_hours
    begin_time = time.fromisoformat(hours["start"])
    end_time = time.fromisoformat(hours["end"])
    day = local.date()
    overnight = end_time <= begin_time
    if overnight and local.timetz().replace(tzinfo=None) < end_time:
        day -= timedelta(days=1)
    begin = datetime.combine(day, begin_time, zone)
    end = datetime.combine(day + timedelta(days=int(overnight)), end_time, zone)
    return begin, end


def query_availability(config, action, *, runner=None):
    """Read at most six explicit primary/resource calendars via the v4 contract.

    The native command's non-paginated ``freebusy_list`` is validated in full.
    We do not expand groups or infer a person behind an unresolved ID.
    """
    start, end = parse_iso(action["start"]), parse_iso(action["end"])
    begin, finish = work_window(config, action["start"])
    covered_start, covered_end = min(start, begin), max(end, finish)
    owner = config.raw["identity"].get("feishu_owner_open_id")
    targets = ([owner] if owner else []) + sorted(
        set(action["attendee_ids"]) - ({owner} if owner else set())
    )
    evidence = {
        "schema_version": 1,
        "source": "feishu_calendar_freebusy_v4",
        "queried_at": iso_now(),
        "covered_start": covered_start.isoformat(),
        "covered_end": covered_end.isoformat(),
        "candidate_start": begin.isoformat(),
        "candidate_end": finish.isoformat(),
        "checked_ids": [],
        "unknown_ids": [] if owner else ["owner_identity_unconfigured"],
        "conflicts": [],
        "alternatives": [],
        "state": "unknown",
    }
    if action.get("rrule"):
        evidence["unknown_ids"].append("recurrence_not_checked")
    busy = []
    for index, target in enumerate(targets):
        if runner is None or index >= 6 or not target.startswith(("ou_", "omm_")):
            evidence["unknown_ids"].append(target)
            continue
        payload = {
            "time_min": covered_start.isoformat(),
            "time_max": covered_end.isoformat(),
            "only_busy": True,
            "include_external_calendar": True,
            "room_id" if target.startswith("omm_") else "user_id": target,
        }
        try:
            response = runner(
                [
                    "calendar",
                    "freebusys",
                    "list",
                    "--as",
                    "user",
                    "--user-id-type",
                    "open_id",
                    "--data",
                    canonical_json(payload),
                ]
            )
            if response.identity != "user" or not isinstance(response.data, dict):
                raise ValueError("untrusted free/busy response")
            intervals = response.data.get("freebusy_list")
            if not isinstance(intervals, list):
                raise TypeError("free/busy response is incomplete")
            current = []
            for interval in intervals:
                if not isinstance(interval, dict):
                    raise TypeError("invalid free/busy interval")
                left, right = (
                    parse_iso(interval["start_time"]),
                    parse_iso(interval["end_time"]),
                )
                if left >= right:
                    raise ValueError("invalid free/busy interval")
                if left < covered_end and right > covered_start:
                    current.append((max(left, covered_start), min(right, covered_end)))
        except (
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OSError,
            TimeoutExpired,
        ):
            evidence["unknown_ids"].append(target)
            continue
        evidence["checked_ids"].append(target)
        busy.extend(current)
        evidence["conflicts"].extend(
            {"attendee_id": target, "start": left.isoformat(), "end": right.isoformat()}
            for left, right in current
            if left < end and right > start
        )
    if evidence["conflicts"]:
        evidence["state"] = "conflict"
    elif not evidence["unknown_ids"] and evidence["checked_ids"]:
        evidence["state"] = "available"
    if evidence["state"] != "conflict" or evidence["unknown_ids"]:
        return evidence
    # Candidates use only a fully covered, explicit local work window. A query
    # error or unknown group suppresses all claims of common availability.
    merged = []
    for left, right in sorted(busy):
        if right <= begin or left >= finish:
            continue
        left, right = max(left, begin), min(right, finish)
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
        else:
            merged.append((left, right))
    cursor, duration = begin, end - start
    cursor = max(cursor, parse_iso(iso_now()))
    zone = ZoneInfo(action["timezone"])
    for left, right in [*merged, (finish, finish)]:
        if left - cursor >= duration:
            evidence["alternatives"].append(
                {
                    "start": cursor.astimezone(zone).isoformat(),
                    "end": (cursor + duration).astimezone(zone).isoformat(),
                }
            )
            if len(evidence["alternatives"]) == 3:
                break
        cursor = max(cursor, right)
    return evidence
