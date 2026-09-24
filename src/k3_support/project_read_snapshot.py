"""Bounded official read normalization, never a write/permission adapter.

Pagination completeness concerns logical fields, not all possible values. Matching
bookends detect some concurrent edits; no server snapshot/CAS contract is claimed.
"""

import json
import math
import time

from .ids import digest
from .project_read_client import ProjectReadError, _string, canonical_host
from .timeutil import iso_now


def _require(condition, code="invalid_snapshot_response"):
    if not condition:
        raise ProjectReadError(code)


def _integer(value, minimum=0, maximum=10000):
    return type(value) is int and minimum <= value <= maximum


def _attributes(value):
    _require(isinstance(value, dict))
    # The live API varies the order of role records keyed by role key. Preserve
    # every member/value, but compare these records by identity, not list order.
    if "role_members" in value:
        roles = value["role_members"]
        _require(
            isinstance(roles, list)
            and all(isinstance(r, dict) and _string(r.get("key")) for r in roles)
        )
        _require(len({r["key"] for r in roles}) == len(roles))
        value = value | {"role_members": sorted(roles, key=lambda r: r["key"])}
    return value


def _role_summary(attributes):
    """Display observed role membership without exposing directory attributes.

    This is not a complete role roster or an authorization/write baseline.
    """
    if "role_members" not in attributes:
        return None
    roles = []
    for row in attributes["role_members"]:
        members = row.get("members")
        known = isinstance(members, list) and len(members) <= 200
        people = []
        if known:
            for member in members:
                key = member if isinstance(member, str) else member.get("key") if isinstance(member, dict) else None
                if not _string(key):
                    known = False
                    break
                name = member.get("name") if isinstance(member, dict) else None
                people.append({"key": key, "name": name if _string(name) else key})
        roles.append({"key": row["key"], "name": row.get("name") if _string(row.get("name")) else row["key"],
                      "members_observed": known, "members": people if known else []})
    return {"roles": roles, "complete_roster": False}


class SnapshotReader:
    def __init__(self, client, *, before_read=None, page_size=100):
        _require(_integer(page_size, 1, 200), "invalid_read_request")
        self.client = client
        self.before_read = before_read
        self.page_size = page_size

    def collect(self, destination):
        _require(
            isinstance(destination, dict)
            and set(destination) == {"host", "project_key", "type_key", "item_id"}
            and all(_string(v) for v in destination.values()),
            "invalid_read_request",
        )
        _require(
            canonical_host(destination["host"])
            == destination["host"]
            == self.client.host,
            "profile_host_mismatch",
        )
        started = iso_now()
        deadline = time.monotonic() + 300
        calls = 0
        byte_count = 0

        def read(command, params):
            nonlocal calls, byte_count
            _require(calls < 64, "snapshot_budget_exceeded")
            _require(time.monotonic() < deadline, "snapshot_budget_exceeded")
            if self.before_read:
                self.before_read()
            calls += 1
            envelope = self.client.read_page(command, params)
            _require(time.monotonic() < deadline, "snapshot_budget_exceeded")
            _require(
                isinstance(envelope, dict)
                and envelope.get("host") == destination["host"]
                and envelope.get("command") == command,
                "snapshot_identity_mismatch",
            )
            data = envelope.get("payload")
            _require(isinstance(data, dict))
            try:
                byte_count += len(json.dumps(data, allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError, UnicodeError, RecursionError):
                raise ProjectReadError("invalid_snapshot_response") from None
            _require(byte_count <= 8 * 1024 * 1024, "snapshot_budget_exceeded")
            return data

        def metadata():
            fields = {}
            total = None
            for number in range(1, 21):
                data = read(
                    "workitem.meta-fields",
                    {
                        "project_key": destination["project_key"],
                        "work_item_type": destination["type_key"],
                        "page_num": number,
                    },
                )
                page = data.get("pagination")
                rows = data.get("list")
                _require(isinstance(page, dict) and isinstance(rows, list))
                _require(type(page.get("has_more")) is bool)
                _require(
                    type(page.get("page_num")) is int and page["page_num"] == number
                )
                _require(type(page.get("page_size")) is int and page["page_size"] == 50)
                _require(_integer(page.get("total"), 1, 1000) and len(rows) <= 50)
                if total is None:
                    total = page["total"]
                _require(total == page["total"], "snapshot_changed_during_read")
                for row in rows:
                    _require(
                        isinstance(row, dict)
                        and all(
                            _string(row.get(k))
                            for k in ("field_key", "field_name", "field_type")
                        )
                    )
                    _require(row["field_key"] not in fields, "duplicate_snapshot_field")
                    fields[row["field_key"]] = row
                if not page["has_more"]:
                    _require(len(fields) == total)
                    return fields, number
                _require(len(rows) == 50 and len(fields) < total)
            raise ProjectReadError("snapshot_budget_exceeded")

        definitions, schema_pages = metadata()
        params = {
            "project_key": destination["project_key"],
            "work_item_id": destination["item_id"],
            "fields": ["_all"],
            "page_size": self.page_size,
        }
        values, names, tokens, seen_fields = {}, {}, set(), set()
        first = None
        attributes = None
        total = None
        for number in range(1, 21):
            data = read("workitem.get", params)
            attr, rows, page = (
                data.get("work_item_attribute"),
                data.get("work_item_fields"),
                data.get("pagination"),
            )
            _require(
                isinstance(attr, dict)
                and isinstance(rows, list)
                and isinstance(page, dict)
            )
            attr = _attributes(attr)
            data = data | {"work_item_attribute": attr}
            _require(
                isinstance(attr.get("owned_project"), dict)
                and attr["owned_project"].get("key") == destination["project_key"]
                and isinstance(attr.get("work_item_type"), dict)
                and attr["work_item_type"].get("key") == destination["type_key"]
                and attr.get("work_item_id") == destination["item_id"],
                "snapshot_identity_mismatch",
            )
            _require(
                isinstance(attr.get("work_item_status"), dict)
                and all(
                    _string(attr["work_item_status"].get(k)) for k in ("key", "name")
                )
                and _string(attr.get("work_item_name"))
                and _string(attr.get("update_time"))
                and _string(attr.get("work_item_mod"))
            )
            _require(
                type(page.get("has_more")) is bool
                and _integer(page.get("total"), 1, 4000)
            )
            _require(
                type(page.get("page_size")) is int
                and page["page_size"] == self.page_size
            )
            _require(len(rows) <= self.page_size)
            if first is None:
                first, attributes, total = data, attr, page["total"]
            _require(
                attr == attributes and total == page["total"],
                "snapshot_changed_during_read",
            )
            for row in rows:
                _require(
                    isinstance(row, dict)
                    and _string(row.get("key"))
                    and _string(row.get("name"))
                )
                _require(row["key"] not in seen_fields, "duplicate_snapshot_field")
                seen_fields.add(row["key"])
                names[row["key"]] = row["name"]
                if "value" in row:
                    values[row["key"]] = row["value"]
            token = page.get("next_page_token")
            if not page["has_more"]:
                _require(
                    token in (None, "") and number == math.ceil(total / self.page_size)
                )
                break
            _require(number < math.ceil(total / self.page_size))
            _require(_string(token) and token not in tokens, "invalid_snapshot_cursor")
            tokens.add(token)
            params = params | {"page_token": token}
        else:
            raise ProjectReadError("snapshot_budget_exceeded")

        # Re-read schema and first item page. No retry loop that might hide edits.
        final_definitions, _ = metadata()
        _require(definitions == final_definitions, "snapshot_changed_during_read")
        again = read(
            "workitem.get", {k: v for k, v in params.items() if k != "page_token"}
        )
        again = again | {
            "work_item_attribute": _attributes(again.get("work_item_attribute"))
        }
        _require(again == first, "snapshot_changed_during_read")
        _require("name" not in values or values["name"] == attributes["work_item_name"])
        values["name"] = attributes["work_item_name"]
        names["name"] = definitions.get("name", {}).get("field_name", "名称")
        # Preserve schema-defined fields for display even when no value was
        # returned. These None entries are placeholders, not observed nulls:
        # unobserved_field_keys below must exclude them from writable baselines.
        unobserved = sorted(set(definitions) - values.keys())
        omitted = sorted(seen_fields - values.keys())
        for key in unobserved:
            values[key] = None
            names[key] = definitions[key]["field_name"]
        from .project_create_schema import _choices

        field_options = {}
        for key, row in definitions.items():
            if row["field_type"] not in {"select", "tree-select"}:
                continue
            try:
                field_options[key] = _choices(
                    row.get("option"), tree=row["field_type"] == "tree-select"
                )
            except ProjectReadError:
                # A malformed optional editor contract must not invent choices
                # or discard otherwise valid read-only item evidence.
                continue
        return {
            "destination": dict(destination),
            "read_started_at": started,
            "observed_at": iso_now(),
            "snapshot": {
                "fields": values,
                "status_id": attributes["work_item_status"]["key"],
                "closure": {
                    "closed": None,
                    "reason": "Remote terminal-state semantics are not classified.",
                },
                "remote_version": None,
                "schema_digest": digest(definitions),
            },
            "read_evidence": {
                "status_name": attributes["work_item_status"]["name"],
                "workflow_mode": attributes["work_item_mod"],
                "update_marker": attributes["update_time"],
                "field_names": names,
                "field_types": {
                    key: row["field_type"] for key, row in definitions.items()
                },
                "field_options": field_options,
                "role_membership": _role_summary(attributes),
                "attachment_fields": {
                    key: {"name": row["field_name"], "type": row["field_type"]}
                    for key, row in definitions.items()
                    if row["field_type"] in {"file", "multi-file"}
                },
                "metadata_field_count": len(definitions),
                "metadata_pages": schema_pages,
                "logical_field_total": total,
                "item_pages": number,
                "pagination_complete": True,
                "bookends_equal": True,
                "atomic_snapshot": False,
                "unobserved_field_keys": unobserved,
                "omitted_value_field_keys": omitted,
                "unknown_schema_field_keys": sorted(
                    (seen_fields | values.keys()) - definitions.keys()
                ),
                "remote_requests": calls,
            },
        }
