"""Compact, read-only workbench addressing; these tokens grant no authority.

The namespace survives restarts and must rotate in the trusted restore flow.
Membership is live below a fixed opening upper bound, not a historical snapshot.
"""

from __future__ import annotations

import base64
import binascii
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

MAX_SEQ = (1 << 48) - 1
VIEW_CODES = {
    "all": "a",
    "needs_me": "m",
    "ai": "i",
    "human": "h",
    "waiting": "w",
    "errors": "e",
    "approvals": "p",
    "knowledge": "k",
    "closed": "c",
}
_VIEWS = tuple(VIEW_CODES)
_CODES = {code: view for view, code in VIEW_CODES.items()}
_LENGTHS = {"wb2": 29, "wi2": 35, "wd2": 45}


@dataclass(frozen=True)
class Navigation:
    instance: bytes
    view: str
    upper: int
    anchor: int = 0
    previous: bool = False


def namespace(conn: sqlite3.Connection) -> bytes:
    try:
        row = conn.execute(
            "SELECT instance_id FROM workbench_navigation_namespace WHERE singleton=1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise ValueError(
            "workbench navigation migration required; refresh after deployment"
        ) from exc
    if row is None or not isinstance(row[0], bytes) or len(row[0]) != 16:
        raise ValueError("invalid workbench namespace; trusted repair required")
    return row[0]


def rotate_namespace(conn: sqlite3.Connection) -> None:
    """Trusted restore/reset hook ONLY. Never call while handling a read/view."""
    namespace(conn)
    conn.execute(
        "UPDATE workbench_navigation_namespace SET instance_id=randomblob(16) WHERE singleton=1"
    )


def open_navigation(conn: sqlite3.Connection, view: str = "all") -> Navigation:
    if view not in VIEW_CODES:
        raise ValueError("unknown workbench view")
    instance = namespace(conn)
    upper = int(
        conn.execute(
            "SELECT coalesce(max(item_seq),0) FROM workbench_item_keys"
        ).fetchone()[0]
    )
    return Navigation(instance, view, upper)


def _base(nav: Navigation) -> bytes:
    if len(nav.instance) != 16 or nav.view not in VIEW_CODES:
        raise ValueError("invalid workbench cursor")
    if not 0 <= nav.anchor <= nav.upper <= MAX_SEQ or (
        nav.previous and nav.anchor == 0
    ):
        raise ValueError("workbench cursor is out of range")
    flags = _VIEWS.index(nav.view) | (16 if nav.previous else 0)
    return (
        nav.instance
        + bytes([flags])
        + nav.upper.to_bytes(6, "big")
        + nav.anchor.to_bytes(6, "big")
    )


def encode(
    nav: Navigation,
    *,
    item_seq: int | None = None,
    page: int | None = None,
    content_digest: str | None = None,
) -> str:
    raw, prefix = _base(nav), "wb2"
    if item_seq is not None:
        if isinstance(item_seq, bool) or not 1 <= item_seq <= nav.upper:
            raise ValueError("workbench target is out of range")
        raw += item_seq.to_bytes(6, "big")
        prefix = "wi2"
    if page is not None or content_digest is not None:
        if (
            item_seq is None
            or isinstance(page, bool)
            or not isinstance(page, int)
            or not 1 <= page <= 65535
        ):
            raise ValueError("workbench detail page is out of range")
        if not isinstance(content_digest, str) or not re.fullmatch(
            r"[a-f0-9]{16}", content_digest
        ):
            raise ValueError("invalid workbench content digest")
        raw += page.to_bytes(2, "big") + bytes.fromhex(content_digest)
        prefix = "wd2"
    return prefix + ":" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(
    conn: sqlite3.Connection, value: str
) -> tuple[Navigation, int | None, int | None, str | None]:
    prefix, separator, encoded = value.partition(":")
    if (
        not separator
        or prefix not in _LENGTHS
        or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded)
    ):
        raise ValueError("unsupported workbench button; refresh the workbench")
    try:
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid workbench cursor; refresh the workbench") from exc
    if (
        len(raw) != _LENGTHS[prefix]
        or base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded
    ):
        raise ValueError("invalid workbench cursor; refresh the workbench")
    flags = raw[16]
    if flags & 224 or flags & 15 >= len(_VIEWS):
        raise ValueError("invalid workbench flags; refresh the workbench")
    nav = Navigation(
        raw[:16],
        _VIEWS[flags & 15],
        int.from_bytes(raw[17:23], "big"),
        int.from_bytes(raw[23:29], "big"),
        bool(flags & 16),
    )
    _base(nav)
    if nav.instance != namespace(conn):
        raise ValueError("workbench database generation changed; refresh the workbench")
    maximum = int(
        conn.execute(
            "SELECT coalesce(max(item_seq),0) FROM workbench_item_keys"
        ).fetchone()[0]
    )
    if nav.upper > maximum:
        raise ValueError(
            "workbench cursor upper bound is invalid; refresh the workbench"
        )
    item_seq = int.from_bytes(raw[29:35], "big") if prefix != "wb2" else None
    page = int.from_bytes(raw[35:37], "big") if prefix == "wd2" else None
    fingerprint = raw[37:45].hex() if prefix == "wd2" else None
    if item_seq is not None and not 1 <= item_seq <= nav.upper:
        raise ValueError("workbench target is out of range")
    if page is not None and page == 0:
        raise ValueError("workbench detail page is out of range")
    return nav, item_seq, page, fingerprint


def validate_origin(conn: sqlite3.Connection, cursor: str) -> str:
    """Return a canonical list cursor, without changing any state or permission."""
    if cursor.startswith("wb2:open:") and cursor.removeprefix("wb2:open:") in _CODES:
        return encode(open_navigation(conn, _CODES[cursor.removeprefix("wb2:open:")]))
    nav, item_seq, _, _ = decode(conn, cursor)
    if item_seq is not None:
        raise ValueError("workbench return must be a list cursor")
    return encode(nav)


def route(conn: sqlite3.Connection, config: Any, callback_data: str) -> dict[str, Any]:
    """Call ONLY after operator/chat validation; navigation does not authorize actions."""
    from .workbench import detail_page, workbench_page, workbench_target

    conn.execute("SAVEPOINT workbench_route_read")
    try:
        if callback_data.startswith("wb2:"):
            return workbench_page(
                conn, config, cursor=validate_origin(conn, callback_data)
            )
        nav, item_seq, page, fingerprint = decode(conn, callback_data)
        origin = encode(nav)
        if page is not None:
            return detail_page(
                conn,
                config,
                item_seq=item_seq,
                page=page,
                expected_digest=fingerprint,
                origin_cursor=origin,
            )
        return workbench_target(conn, config, item_seq=item_seq, origin_cursor=origin)
    finally:
        conn.execute("RELEASE workbench_route_read")
