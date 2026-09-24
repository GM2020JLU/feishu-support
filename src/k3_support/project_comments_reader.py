"""Official flat comment observations; no guessed reply hierarchy or URL fetching."""

import json
import time

from .ids import digest
from .project_history_reader import _timestamp
from .project_read_client import ProjectReadError, _string
from .timeutil import iso_now


def _require(condition, code="invalid_comments_response"):
    if not condition:
        raise ProjectReadError(code)


def normalize_page(payload, *, number):
    _require(isinstance(payload, dict))
    rows, page = payload.get("comments"), payload.get("pagination")
    _require(isinstance(rows, list) and len(rows) <= 20 and isinstance(page, dict))
    _require(
        type(page.get("page_num")) is int
        and page["page_num"] == number
        and type(page.get("page_size")) is int
        and page["page_size"] == 20
    )
    total, pages = page.get("total"), page.get("total_pages")
    _require(
        type(total) is int
        and 0 <= total <= 100000
        and type(pages) is int
        and pages == (total + 19) // 20
    )
    _require(
        (total == 0 and number == 1 and not rows)
        or (0 < number <= pages and len(rows) == min(20, total - (number - 1) * 20))
    )
    items = []
    for row in rows:
        # Reject a future reply-bearing shape rather than silently discarding it.
        _require(
            isinstance(row, dict)
            and set(row)
            == {"comment_id", "content", "created_at", "creator", "file_url"}
        )
        _require(all(_string(row[k]) for k in ("comment_id", "created_at", "creator")))
        _require(
            isinstance(row["content"], str)
            and len(row["content"].encode("utf-8")) <= 65536
        )
        _require(isinstance(row["file_url"], str) and len(row["file_url"]) <= 8192)
        items.append(
            {
                "comment_id": row["comment_id"],
                "content": row["content"],
                "created_at_display": row["created_at"],
                "creator": row["creator"],
                "attachment_reference": row["file_url"],
                "record_digest": digest(row),
            }
        )
    _require(len({i["comment_id"] for i in items}) == len(items), "duplicate_comment")
    return {"items": items, "total": total, "pages": pages, "has_more": number < pages}


class CommentsReader:
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
        started, deadline, byte_count, calls = iso_now(), time.monotonic() + 300, 0, 0

        def read(number):
            nonlocal byte_count, calls
            _require(
                calls < 21 and time.monotonic() < deadline, "comments_budget_exceeded"
            )
            if self.before_read:
                self.before_read()
            envelope = self.client.read_page(
                "comment.list",
                {
                    "project_key": destination["project_key"],
                    "work_item_id": destination["item_id"],
                    "page_num": number,
                    "end_time": end,
                },
            )
            calls += 1
            _require(time.monotonic() < deadline, "comments_budget_exceeded")
            _require(
                isinstance(envelope, dict)
                and envelope.get("host") == destination["host"]
                and envelope.get("command") == "comment.list",
                "comments_identity_mismatch",
            )
            payload = envelope.get("payload")
            try:
                byte_count += len(
                    json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(
                        "utf-8"
                    )
                )
                _require(byte_count <= 8 * 1024 * 1024, "comments_budget_exceeded")
                return normalize_page(payload, number=number)
            except (TypeError, ValueError, UnicodeError, RecursionError):
                raise ProjectReadError("invalid_comments_response") from None

        first, items, seen = None, [], set()
        for number in range(1, 21):
            page = read(number)
            if first is None:
                first = page
            _require(
                (page["total"], page["pages"]) == (first["total"], first["pages"]),
                "comments_changed_during_read",
            )
            for item in page["items"]:
                _require(item["comment_id"] not in seen, "duplicate_comment")
                seen.add(item["comment_id"])
                items.append(item)
            if not page["has_more"]:
                break
        else:
            raise ProjectReadError("comments_budget_exceeded")
        check = read(1)
        _require(check == first, "comments_changed_during_read")
        _require(len(items) == first["total"])
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
            "identity_binding": "request_parameters",
            "reply_hierarchy_verified": False,
            "authority": "comment_observation_only",
        }
