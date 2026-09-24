"""Read source-Bug relation definitions and edge summaries, without graph expansion.

Names come from official metadata. Version roles are not guessed from field names,
and reading a relation never grants authority to inspect or modify its target.
"""

import json
import time

from .ids import digest
from .project_read_client import ProjectReadError, _string
from .timeutil import iso_now


def _require(condition, code="invalid_relations_response"):
    if not condition:
        raise ProjectReadError(code)


def definitions(payload, destination):
    _require(isinstance(payload, dict) and isinstance(payload.get("list"), list))
    _require(len(payload["list"]) <= 50, "relations_budget_exceeded")
    result, seen = [], set()
    for row in payload["list"]:
        _require(
            isinstance(row, dict)
            and all(
                _string(row.get(k))
                for k in ("id", "name", "work_item_type_key", "work_item_type_name")
            )
        )
        _require(
            row["work_item_type_key"] == destination["type_key"],
            "relations_identity_mismatch",
        )
        _require(
            row["id"] not in seen
            and type(row.get("disabled")) is bool
            and type(row.get("relation_type")) is int
        )
        seen.add(row["id"])
        details = row.get("relation_details")
        _require(isinstance(details, list) and 1 <= len(details) <= 50)
        targets, pairs = [], set()
        for target in details:
            keys = (
                "project_key",
                "project_name",
                "work_item_type_key",
                "work_item_type_name",
            )
            _require(
                isinstance(target, dict) and all(_string(target.get(k)) for k in keys)
            )
            pair = (target["project_key"], target["work_item_type_key"])
            _require(pair not in pairs)
            pairs.add(pair)
            targets.append({k: target[k] for k in keys})
        result.append(
            {
                "relation_id": row["id"],
                "relation_name": row["name"],
                "relation_type": row["relation_type"],
                "disabled": row["disabled"],
                "target_types": sorted(
                    targets, key=lambda t: (t["project_key"], t["work_item_type_key"])
                ),
            }
        )
    return sorted(result, key=lambda d: d["relation_id"])


def relation_page(payload, definition, *, number):
    _require(isinstance(payload, dict) and isinstance(payload.get("pagination"), dict))
    page, rows = payload["pagination"], payload.get("list")
    _require(
        type(page.get("page_num")) is int
        and page["page_num"] == number
        and type(page.get("page_size")) is int
        and page["page_size"] == 50
        and type(page.get("total")) is int
        and 0 <= page["total"] <= 10000
    )
    total = page["total"]
    if rows is None:
        _require(total == 0 and number == 1)
        rows = []
    _require(isinstance(rows, list) and len(rows) <= 50)
    _require(
        (total == 0 and number == 1 and not rows)
        or (
            total > 0
            and 1 <= number <= (total + 49) // 50
            and len(rows) == min(50, total - (number - 1) * 50)
        )
    )
    allowed = {
        (t["project_key"], t["work_item_type_key"]): t
        for t in definition["target_types"]
    }
    result, seen = [], set()
    for row in rows:
        _require(
            isinstance(row, dict)
            and type(row.get("id")) is int
            and 0 < row["id"] <= 2**63 - 1
        )
        _require(
            all(
                _string(row.get(k))
                for k in ("name", "project_key", "work_item_type_key")
            )
        )
        pair = (row["project_key"], row["work_item_type_key"])
        _require(pair in allowed, "relations_identity_mismatch")
        key = (*pair, str(row["id"]))
        _require(key not in seen, "duplicate_relation_target")
        seen.add(key)
        result.append(
            {
                "item_id": str(row["id"]),
                "name": row["name"],
                "project_key": pair[0],
                "type_key": pair[1],
                "project_name": allowed[pair]["project_name"],
                "type_name": allowed[pair]["work_item_type_name"],
            }
        )
    return {"items": result, "total": total, "has_more": number * 50 < total}


def _page_signature(page):
    return digest(
        page
        | {
            "items": sorted(
                page["items"],
                key=lambda t: (t["project_key"], t["type_key"], t["item_id"]),
            )
        }
    )


class RelationsReader:
    def __init__(self, client, *, before_read=None):
        self.client, self.before_read = client, before_read

    def collect(self, destination, *, end):
        _require(
            isinstance(destination, dict)
            and set(destination) == {"host", "project_key", "type_key", "item_id"}
            and all(_string(v) for v in destination.values()),
            "invalid_read_request",
        )
        _require(destination["host"] == self.client.host, "profile_host_mismatch")
        # The relation API has no time filter: do not claim submission cutoff.
        started, deadline, calls, byte_count = iso_now(), time.monotonic() + 300, 0, 0

        def read(command, params):
            nonlocal calls, byte_count
            _require(
                calls < 128 and time.monotonic() < deadline, "relations_budget_exceeded"
            )
            if self.before_read:
                self.before_read()
            envelope = self.client.read_page(command, params)
            calls += 1
            _require(time.monotonic() < deadline, "relations_budget_exceeded")
            _require(
                isinstance(envelope, dict)
                and envelope.get("host") == destination["host"]
                and envelope.get("command") == command,
                "relations_identity_mismatch",
            )
            payload = envelope.get("payload")
            try:
                byte_count += len(
                    json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(
                        "utf-8"
                    )
                )
            except (TypeError, ValueError, UnicodeError, RecursionError):
                raise ProjectReadError("invalid_relations_response") from None
            _require(byte_count <= 8 * 1024 * 1024, "relations_budget_exceeded")
            return payload

        meta_params = {
            "project_key": destination["project_key"],
            "work_item_type": destination["type_key"],
        }
        source = {
            "project_key": destination["project_key"],
            "work_item_id": destination["item_id"],
            "page_size": 50,
        }
        schema = definitions(
            read("relation.meta-definitions", meta_params), destination
        )
        first_pages, items, relation_pages, edge_count = {}, [], 0, 0
        for definition in schema:
            common = {
                k: definition[k]
                for k in ("relation_id", "relation_name", "relation_type")
            }
            if definition["disabled"]:
                items.append(common | {"state": "disabled", "target": None})
                continue
            targets, seen, first = [], set(), None
            for number in range(1, 21):
                page = relation_page(
                    read(
                        "relation.list",
                        source
                        | {
                            "relation_id": definition["relation_id"],
                            "page_num": number,
                        },
                    ),
                    definition,
                    number=number,
                )
                relation_pages += 1
                if first is None:
                    first = page
                    first_pages[definition["relation_id"]] = _page_signature(page)
                _require(
                    page["total"] == first["total"], "relations_changed_during_read"
                )
                for target in page["items"]:
                    key = (target["project_key"], target["type_key"], target["item_id"])
                    _require(key not in seen, "duplicate_relation_target")
                    seen.add(key)
                    targets.append(target)
                if not page["has_more"]:
                    break
            else:
                raise ProjectReadError("relations_budget_exceeded")
            _require(len(targets) == first["total"])
            edge_count += len(targets)
            if targets:
                items.extend(common | {"state": "linked", "target": t} for t in targets)
            else:
                items.append(common | {"state": "empty", "target": None})
            _require(len(items) <= 10000, "relations_budget_exceeded")
        current = definitions(
            read("relation.meta-definitions", meta_params), destination
        )
        _require(current == schema, "relations_changed_during_read")
        for definition in schema:
            if definition["disabled"]:
                continue
            page = relation_page(
                read(
                    "relation.list",
                    source | {"relation_id": definition["relation_id"], "page_num": 1},
                ),
                definition,
                number=1,
            )
            _require(
                _page_signature(page) == first_pages[definition["relation_id"]],
                "relations_changed_during_read",
            )
        return {
            "items": items,
            "observed_at": iso_now(),
            "read_started_at": started,
            "end_time_ms": None,
            "record_count": len(items),
            "definition_count": len(schema),
            "target_count": edge_count,
            "pages": relation_pages,
            "disabled_definitions": sum(d["disabled"] for d in schema),
            "pagination_complete": True,
            "bookends_equal": True,
            "atomic_snapshot": False,
            "definition_scope": "returned_to_current_identity",
            "target_details_fetched": False,
            "authority": "relation_observation_only",
        }
