"""Exact link-intake scope, independently opted in by control configuration."""

import re
from urllib.parse import urlsplit

from .project_read_client import _string
from .project_reader_config import selected


def validate(spaces):
    if not isinstance(spaces, list) or len(spaces) > 20:
        raise ValueError("intake spaces must be a bounded list")
    seen = set()
    for space in spaces:
        required = {
            "simple_name",
            "project_key",
            "type_keys",
        }
        if (
            not isinstance(space, dict)
            or not required <= set(space)
            or set(space) - required - {"type_aliases"}
        ):
            raise ValueError("intake space requires exact fields")
        if (
            not _string(space["simple_name"])
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", space["simple_name"])
            or not _string(space["project_key"])
        ):
            raise ValueError("invalid intake space identity")
        types = space["type_keys"]
        if (
            not isinstance(types, list)
            or not 1 <= len(types) <= 20
            or any(
                not isinstance(t, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", t)
                for t in types
            )
        ):
            raise ValueError("invalid intake type keys")
        aliases = space.get("type_aliases", {})
        if (
            not isinstance(aliases, dict)
            or len(aliases) > 20
            or any(
                not isinstance(alias, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", alias)
                or not isinstance(canonical, str)
                or canonical not in types
                or alias in types
                for alias, canonical in aliases.items()
            )
        ):
            raise ValueError("invalid intake type aliases")
        for key in [*types, *aliases]:
            pair = (space["simple_name"], key)
            if pair in seen:
                raise ValueError("duplicate intake space/type")
            seen.add(pair)
    return spaces


def parse(config, url):
    reader = selected(config)
    if reader is None or not _string(url):
        raise ValueError("Project link intake is unavailable")
    parts = urlsplit(url)
    match = re.fullmatch(
        r"/([A-Za-z0-9_-]{1,128})/([A-Za-z0-9_-]{1,128})/detail/([0-9]{1,32})",
        parts.path,
    )
    if (
        parts.scheme != "https"
        or parts.netloc != reader["host"]
        or parts.query
        or parts.fragment
        or not match
        or url != "https://" + reader["host"] + parts.path
    ):
        raise ValueError("use a clean approved Project Bug detail URL")
    slug, kind, item = match.groups()
    spaces = validate(
        config.raw.get("project_integration", {}).get("intake_spaces", [])
    )
    allowed = next(
        (
            s
            for s in spaces
            if s["simple_name"] == slug
            and (kind in s["type_keys"] or kind in s.get("type_aliases", {}))
        ),
        None,
    )
    if allowed is None:
        raise PermissionError("Project space/type is outside link-intake scope")
    canonical = allowed.get("type_aliases", {}).get(kind, kind)
    return {
        "host": reader["host"],
        "simple_name": slug,
        "type_key": canonical,
        "item_id": item,
        "project_key": allowed["project_key"],
    } | ({"url_type_key": kind} if canonical != kind else {})
