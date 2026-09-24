"""Version/related-item edits reuse creation metadata and scoped target reads."""

from . import project_bug_grants as grants
from . import project_create_related as related
from .project_bugs import _bug, detail
from .project_read_client import MeegleReadClient


def ids(kind, value, *, observed=False):
    """Accepted IDs only; labels never establish the identity of a relation."""
    multi = related.TYPES[kind] == "workitem_related_multi_select"
    if value is None:
        return []
    if observed:
        rows = value if multi else [value]
        if not isinstance(rows, list) or any(
            not isinstance(x, dict)
            or set(x) != {"id", "name"}
            or not isinstance(x["name"], str)
            for x in rows
        ):
            raise ValueError("related field value is not an accepted observation")
        result = [x["id"] for x in rows]
    elif multi:
        result = value
    else:
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            raise ValueError("single related field requires an ID string")
        result = [int(value)]
    if (
        not isinstance(result, list)
        or len(result) > 20
        or any(type(x) is not int or not 1 <= x <= 2**53 - 1 for x in result)
        or len(set(result)) != len(result)
    ):
        raise ValueError("invalid related field IDs")
    return sorted(result)


def validate_values(client, config, destination, evidence, values, guard):
    """Validate every selected target just before issuing an ordinary field write."""
    checks = []
    for key, value in values.items():
        kind = evidence["field_types"].get(key)
        if kind not in related.TYPES:
            continue
        if config is None:
            raise PermissionError(
                "related field writes require configured target read scopes"
            )
        selected = ids(kind, value)
        guard()
        bound = related.target(
            client, destination, {"field_key": key, "field_type_key": kind}
        )
        scope = related.authorized(config, bound)
        if scope["allowed_item_ids"] is not None and not set(selected) <= set(
            scope["allowed_item_ids"]
        ):
            raise PermissionError("related item is outside configured read scope")
        if selected:
            guard()
            result = client.query_bugs(scope | {"allowed_item_ids": selected})
            if (
                result.get("host") != destination["host"]
                or result.get("next_after_id") is not None
                or len(result.get("items", [])) != len(selected)
                or {row["item_id"] for row in result["items"]}
                != {str(x) for x in selected}
            ):
                raise ValueError("related items no longer resolve")
        checks.append((bound, scope))
    guard()
    for bound, scope in checks:
        if related.authorized(config, bound) != scope:
            raise PermissionError("related target scope changed during write preflight")


def search(
    conn, config, *, actor, bug_id, grant_id, field_key, query, client_factory=None
):
    from .project_refresh import _fingerprint, _selection

    if not isinstance(query, str) or not query.strip() or len(query) > 128:
        raise ValueError("provide a related item name")
    bug = _bug(conn, bug_id)
    destination = {k: bug[k] for k in ("host", "project_key", "type_key", "item_id")}
    target = {k: bug[k] for k in ("host", "project_key", "type_key", "bug_id")}
    target.update(
        action="bug.fields",
        fields=[field_key],
        transition=None,
        repository=None,
        device=None,
    )

    def guard():
        if not grants.covers(conn, grant_id=grant_id, actor=actor, target=target):
            raise PermissionError("related lookup is outside current field grant")
        return _selection(conn, config, destination, actor)

    reader = guard()
    stamp = _fingerprint(conn, config, reader)
    snapshot = detail(conn, bug_id)["snapshot"]
    evidence = snapshot.get("read_evidence", {}) if snapshot else {}
    kind = evidence.get("field_types", {}).get(field_key)
    if (
        kind not in related.TYPES
        or field_key not in snapshot["fields"]
        or field_key in evidence.get("unobserved_field_keys", [])
        or field_key in evidence.get("omitted_value_field_keys", [])
    ):
        raise ValueError("field must have an observed related-item baseline")
    ids(kind, snapshot["fields"][field_key], observed=True)
    client = (
        client_factory
        or (
            lambda r: MeegleReadClient(**{k: v for k, v in r.items() if k != "enabled"})
        )
    )(reader)
    guard()
    bound = related.target(
        client, destination, {"field_key": field_key, "field_type_key": kind}
    )
    scope = related.authorized(config, bound)
    guard()
    result = client.query_bugs(scope, keyword=query.strip())
    if (
        _fingerprint(conn, config, guard()) != stamp
        or related.authorized(config, bound) != scope
    ):
        raise PermissionError("related lookup authority changed while reading")
    if result.get("host") != destination["host"]:
        raise ValueError("related query returned another host")
    return {
        "options": [
            {"value": r["item_id"], "label": r["title"]} for r in result["items"]
        ],
        "narrow_query": result.get("next_after_id") is not None,
    }
