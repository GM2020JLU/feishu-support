"""Typed, bounded Project MQL reads. Never accept caller SQL or session IDs.

The owner supplies an authorized scope; this module does not confer permission.
Each page is a fresh observation, not a consistent multi-page snapshot.
"""

import re

from .project_read_client import ProjectReadError

PAGE_SIZE = 50
MAX_ID = 2**63 - 1


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)


def item_id(value, *, zero=False):
    return type(value) is int and (0 if zero else 1) <= value <= MAX_ID


def validate_scope(scope):
    required = {"simple_name", "project_key", "type_key", "allowed_item_ids"}
    if (
        not isinstance(scope, dict)
        or not required <= set(scope)
        or set(scope) - required - {"url_type_key"}
        or not all(
            identifier(scope[k]) for k in ("simple_name", "project_key", "type_key")
        )
    ):
        raise ValueError("invalid Project search scope")
    if "url_type_key" in scope and not identifier(scope["url_type_key"]):
        raise ValueError("invalid Project URL type alias")
    ids = scope["allowed_item_ids"]
    if ids is not None and (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 200
        or not all(item_id(i) for i in ids)
        or len(ids) != len(set(ids))
    ):
        raise ValueError(
            "search requires explicit item IDs or explicit whole-type scope"
        )
    return scope


def validate_spaces(spaces):
    if not isinstance(spaces, list) or len(spaces) > 20:
        raise ValueError("search scopes must be a bounded list")
    seen = set()
    for scope in spaces:
        validate_scope(scope)
        pair = (scope["simple_name"], scope["type_key"])
        if pair in seen:
            raise ValueError("duplicate search scope")
        seen.add(pair)
    return spaces


def compile_query(scope, *, keyword="", after_id=0):
    validate_scope(scope)
    if (
        not isinstance(keyword, str)
        or len(keyword) > 200
        or keyword != keyword.strip()
        or any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in keyword)
        or not item_id(after_id, zero=True)
    ):
        raise ValueError("invalid Project search filter")
    where = [f"`work_item_id` > {after_id}"]
    if scope["allowed_item_ids"] is not None:
        where.append(
            "`work_item_id` IN ("
            + ", ".join(map(str, sorted(scope["allowed_item_ids"])))
            + ")"
        )
    if keyword:
        literal = keyword.replace("'", "''").replace("%", "\\%").replace("_", "\\_")
        where.append("`name` LIKE '%" + literal + "%'")
    return {
        "project_key": scope["project_key"],
        "mql": "SELECT `work_item_id`, `name`, `work_item_status` FROM "
        f"`{scope['project_key']}`.`{scope['type_key']}` WHERE "
        + " AND ".join(where)
        + f" ORDER BY `work_item_id` ASC LIMIT {PAGE_SIZE}",
    }


def normalize(payload, scope, *, after_id=0):
    """Reject unordered, duplicate, out-of-scope or incomplete remote responses."""
    validate_scope(scope)
    if not item_id(after_id, zero=True):
        raise ValueError("invalid Project cursor")

    def invalid():
        raise ProjectReadError("invalid_query_response")

    if (
        not isinstance(payload, dict)
        or not {"data", "list", "search_status_info"} <= payload.keys()
        or payload["search_status_info"] is not None
    ):
        invalid()
    data = payload.get("data")
    if not isinstance(data, dict) or set(data) - {"1"}:
        invalid()
    rows = data.get("1", [])
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
        invalid()
    # The native query returns one ungrouped result. Do not turn a partial or
    # different group into a seemingly complete page; ignore session IDs.
    groups = payload.get("list")
    if rows:
        if (
            not isinstance(groups, list)
            or len(groups) != 1
            or not isinstance(groups[0], dict)
        ):
            invalid()
        group = groups[0]
        infos = group.get("group_infos")
        if (
            type(group.get("count")) is not int
            or group["count"] != len(rows)
            or not isinstance(infos, list)
            or len(infos) != 1
            or not isinstance(infos[0], dict)
            or infos[0].get("group_id") != "1"
        ):
            invalid()
    elif groups is not None:
        invalid()
    result, previous = [], after_id
    for row in rows:
        if not isinstance(row, dict) or not isinstance(
            row.get("moql_field_list"), list
        ):
            invalid()
        fields = {}
        for field in row["moql_field_list"]:
            if (
                not isinstance(field, dict)
                or not isinstance(field.get("key"), str)
                or field["key"] in fields
            ):
                invalid()
            kind = field.get("value_type")
            value = field.get("value")
            if (
                not isinstance(kind, str)
                or not isinstance(value, dict)
                or set(value) != {kind}
            ):
                invalid()
            fields[field["key"]] = (kind, value[kind])
        if set(fields) != {"work_item_id", "name", "work_item_status"}:
            invalid()
        id_kind, key = fields["work_item_id"]
        name_kind, name = fields["name"]
        state_kind, states = fields["work_item_status"]
        if (
            id_kind != "long_value"
            or not item_id(key)
            or key <= previous
            or (
                scope["allowed_item_ids"] is not None
                and key not in scope["allowed_item_ids"]
            )
            or name_kind != "string_value"
            or not isinstance(name, str)
            or not 1 <= len(name) <= 4096
            or state_kind != "key_label_value_list"
            or not isinstance(states, list)
            or len(states) != 1
        ):
            invalid()
        state = states[0]
        if not isinstance(state, dict) or not all(
            isinstance(state.get(k), str) and 0 < len(state[k]) <= 1024
            for k in ("key", "label")
        ):
            invalid()
        result.append(
            {
                "item_id": str(key),
                "title": name,
                "status": {"key": state["key"], "label": state["label"]},
            }
        )
        previous = key
    return {
        "items": result,
        "next_after_id": previous if len(rows) == PAGE_SIZE else None,
        "completeness": "page_only",
        "snapshot_consistent": False,
    }
