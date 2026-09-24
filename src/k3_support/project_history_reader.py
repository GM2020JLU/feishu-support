"""Bounded official operation history, without inferring the Bug's current state.

The native endpoint can report total=0 alongside nonempty records. Only verified
has_more/cursor traversal establishes pagination exhaustion, never that counter.
"""

import json
import time

from .ids import digest
from .project_read_client import ProjectReadError, _string
from .timeutil import iso_now

COMMAND = "workitem.list-op-records"
MAX_TIMESTAMP = 253402300799999


def _require(condition, code="invalid_history_response"):
    if not condition:
        raise ProjectReadError(code)


def _timestamp(value):
    return type(value) is int and 0 <= value <= MAX_TIMESTAMP


def normalize_page(payload, destination, *, end):
    _require(isinstance(payload, dict))
    rows = payload.get("op_records")
    _require(isinstance(rows, list) and len(rows) <= 500)
    _require(type(payload.get("has_more")) is bool)
    cursor = payload.get("start_from")
    _require(isinstance(cursor, str) and len(cursor) <= 2048)
    if payload["has_more"]:
        _require(bool(rows) and _string(cursor))
    total = payload.get("total")
    _require(type(total) is int and 0 <= total <= 2**63 - 1)
    items = []
    for row in rows:
        _require(isinstance(row, dict))
        _require(
            row.get("project_key") == destination["project_key"]
            and type(row.get("work_item_id")) is int
            and str(row["work_item_id"]) == destination["item_id"]
            and row.get("work_item_type_key") == destination["type_key"],
            "history_identity_mismatch",
        )
        _require(_timestamp(row.get("operation_time")) and row["operation_time"] <= end)
        _require(
            all(
                _string(row.get(k))
                for k in (
                    "op_record_module",
                    "operation_type",
                    "operator",
                    "operator_type",
                )
            )
        )
        contents = row.get("record_contents")
        _require(isinstance(contents, list) and len(contents) <= 200)
        _require(all(isinstance(c, dict) for c in contents))
        # Preserve opaque content instead of interpreting unknown field types or
        # turning historical changes into authority for another remote operation.
        _require(
            len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            <= 65536
        )
        items.append(
            {
                "record_digest": digest(row),
                "operation_time_ms": row["operation_time"],
                "module": row["op_record_module"],
                "action": row["operation_type"],
                "operator_key": row["operator"],
                "operator_type": row["operator_type"],
                "contents": contents,
                "source": row.get("source"),
                "source_type": row.get("source_type"),
            }
        )
    return {
        "items": items,
        "has_more": payload["has_more"],
        "cursor": cursor,
        "reported_total": total,
    }


class HistoryReader:
    def __init__(self, client, *, before_read=None):
        self.client, self.before_read = client, before_read

    def collect(self, destination, *, end):
        _require(
            isinstance(destination, dict)
            and set(destination) == {"host", "project_key", "type_key", "item_id"}
            and all(_string(v) for v in destination.values())
            and _timestamp(end),
            "invalid_read_request",
        )
        _require(destination["host"] == self.client.host, "profile_host_mismatch")
        started, deadline = iso_now(), time.monotonic() + 300
        calls = byte_count = 0
        params = {
            "project_key": destination["project_key"],
            "work_item_id": destination["item_id"],
            "start": 1,
            "end": end,
        }

        def read(request):
            nonlocal calls, byte_count
            _require(
                calls < 21 and time.monotonic() < deadline, "history_budget_exceeded"
            )
            if self.before_read:
                self.before_read()
            envelope = self.client.read_page(COMMAND, request)
            calls += 1
            _require(time.monotonic() < deadline, "history_budget_exceeded")
            _require(
                isinstance(envelope, dict)
                and envelope.get("host") == destination["host"]
                and envelope.get("command") == COMMAND,
                "history_identity_mismatch",
            )
            payload = envelope.get("payload")
            try:
                byte_count += len(
                    json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(
                        "utf-8"
                    )
                )
                _require(byte_count <= 8 * 1024 * 1024, "history_budget_exceeded")
                return normalize_page(payload, destination, end=end)
            except (TypeError, ValueError, UnicodeError, RecursionError):
                raise ProjectReadError("invalid_history_response") from None

        items, totals, tokens, pages = [], [], set(), set()
        first = None
        request = dict(params)
        for number in range(1, 21):
            page = read(request)
            if first is None:
                first = page
            fingerprint = digest(page["items"])
            _require(fingerprint not in pages, "history_repeated_page")
            pages.add(fingerprint)
            items.extend(page["items"])
            totals.append(page["reported_total"])
            if not page["has_more"]:
                break
            _require(page["cursor"] not in tokens, "history_repeated_cursor")
            tokens.add(page["cursor"])
            request = params | {"start_from": page["cursor"]}
        else:
            raise ProjectReadError("history_budget_exceeded")
        check = read(params)
        _require(
            check["items"] == first["items"] and check["has_more"] == first["has_more"],
            "history_changed_during_read",
        )
        return {
            "items": items,
            "observed_at": iso_now(),
            "read_started_at": started,
            "end_time_ms": end,
            "record_count": len(items),
            "pages": number,
            "pagination_complete": True,
            "bookends_equal": True,
            "atomic_snapshot": False,
            "reported_totals": totals,
            "authority": "historical_observation_only",
        }
