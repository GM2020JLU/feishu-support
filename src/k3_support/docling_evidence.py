"""Import Docling JSON as untrusted derived evidence, never reviewed knowledge.

No converter, URL fetch, model loading, database access or publication occurs here.
The original document is retained rather than flattening tables into lossy text.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_BYTES = 16 * 1024 * 1024
MAX_NODES = 100_000
_COLLECTIONS = ("texts", "tables", "pictures", "key_value_items", "groups")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def import_document(
    raw: bytes,
    *,
    source_id: str,
    source_version: str,
    source_sha256: str,
    parser_version: str,
    model_revision: str,
) -> dict[str, Any]:
    """Build an inert review artifact from a bounded DoclingDocument export.

    Source hash and parser/model revisions are caller declarations, not verified
    attestations. An absent model must explicitly be recorded as ``not-used``.
    Consumers must not interpret document fields as permissions or instructions.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("Docling JSON must be nonempty bytes within 16 MiB")
    for value in (source_id, source_version, parser_version, model_revision):
        if not isinstance(value, str) or not value.strip() or len(value) > 2048:
            raise ValueError(
                "source and parser metadata must be explicit bounded strings"
            )
    if not isinstance(source_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", source_sha256
    ):
        raise ValueError("source_sha256 must be lowercase SHA-256")
    try:
        doc = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Docling JSON") from exc
    if not isinstance(doc, dict) or doc.get("schema_name") != "DoclingDocument":
        raise ValueError("expected DoclingDocument schema")
    version = doc.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"1\.\d+\.\d+", version):
        raise ValueError("unsupported Docling document schema version")

    nodes: dict[str, dict[str, Any]] = {}
    for name in _COLLECTIONS:
        items = doc.get(name, [])
        if not isinstance(items, list) or len(nodes) + len(items) > MAX_NODES:
            raise ValueError("invalid or oversized document collection")
        for index, item in enumerate(items):
            ref = f"#/{name}/{index}"
            if not isinstance(item, dict) or item.get("self_ref") != ref:
                raise ValueError("invalid document self reference")
            nodes[ref] = item
    for name in ("body", "furniture"):
        item = doc.get(name)
        if not isinstance(item, dict) or item.get("self_ref") != f"#/{name}":
            raise ValueError("missing document root")
        nodes[f"#/{name}"] = item

    # Validate all child edges, including unreachable items. Never resolve remote
    # references or treat JSON pointers as filesystem paths.
    edges: dict[str, list[str]] = {}
    for ref, item in nodes.items():
        children = item.get("children", [])
        if not isinstance(children, list):
            raise ValueError("invalid document children field")  # noqa: TRY004 -- invalid external JSON
        targets = []
        for child in children:
            target = child.get("$ref") if isinstance(child, dict) else None
            if not isinstance(target, str) or target not in nodes:
                raise ValueError("unresolved document child reference")
            targets.append(target)
        edges[ref] = targets

    orders: dict[str, list[str]] = {}
    visited: set[str] = set()
    for root in ("body", "furniture"):
        order = []
        stack = [f"#/{root}"]
        while stack:
            ref = stack.pop()
            if ref in visited:
                raise ValueError("cycle or shared child in document tree")
            visited.add(ref)
            if ref != f"#/{root}":
                order.append(ref)
            stack.extend(reversed(edges[ref]))
        orders[root] = order
    if len(visited) != len(nodes):
        raise ValueError("orphan document items require review before import")

    return {
        "schema": "k3-docling-derived-evidence-v1",
        "status": "unreviewed",
        "automatic_reply_eligible": False,
        "source": {"id": source_id, "version": source_version, "sha256": source_sha256},
        "parser": {
            "name": "docling",
            "version": parser_version,
            "model_revision": model_revision,
            "metadata_verified": False,
        },
        "export_sha256": hashlib.sha256(raw).hexdigest(),
        "reading_order": orders,
        "document": doc,
    }
