"""Session-bound GUI imports; does not sign releases or enable automatic replies."""

import json
from datetime import UTC, datetime, timedelta

from .db import transaction
from .ids import canonical_json, digest, new_id
from .professional_knowledge import (
    _import_loaded_bundle,
    _plan_loaded_import,
    validate_bundle,
)
from .timeutil import iso_now, parse_iso


def _state(conn):
    # Conservative binding: even a source refresh invalidates a prepared import.
    return digest(
        {
            table: [
                dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]
            for table in (
                "knowledge_entries",
                "source_registry",
                "professional_knowledge_revisions",
            )
        }
    )


def preview(conn, *, bundle, session_id):
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("缺少登录会话")
    encoded = canonical_json(bundle)
    if len(encoded.encode()) > 500_000:
        raise ValueError("知识包过大，请拆分后审核")
    bundle = validate_bundle(json.loads(encoded))
    if not bundle["entries"]:
        raise ValueError("知识包为空")
    with transaction(conn):
        plan = _plan_loaded_import(conn, bundle)
        changes = []
        previous_bytes = 0
        for entry in bundle["entries"]:
            metadata = entry["metadata"]
            old = conn.execute(
                """SELECT p.revision_number,p.payload_json,p.body_markdown,
                          k.status,p.lifecycle_state
                   FROM knowledge_entries k JOIN professional_knowledge_revisions p
                     ON p.revision_id=k.professional_revision_id
                   WHERE p.stable_id=?""",
                (metadata["id"],),
            ).fetchone()
            previous = None
            if old is not None:
                previous_bytes += len(old["payload_json"].encode()) + len(
                    old["body_markdown"].encode()
                )
                if previous_bytes > 500_000:
                    raise ValueError("旧版本内容过多，请拆分知识包后预览")
                previous = {
                    "metadata": json.loads(old["payload_json"]),
                    "body_markdown": old["body_markdown"],
                    "entry_status": old["status"],
                    "revision_state": old["lifecycle_state"],
                }
            changes.append(
                {
                    "id": metadata["id"],
                    "action": "create"
                    if old is None
                    else "unchanged"
                    if old["revision_number"] == metadata["revision"]
                    else "update",
                    "previous": previous,
                    "proposed_revision": metadata["revision"],
                }
            )
        draft_id = new_id("kid")
        until = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
        conn.execute(
            "INSERT INTO knowledge_import_drafts VALUES(?,?,?,?,?,NULL,NULL,NULL)",
            (draft_id, session_id, _state(conn), encoded, until),
        )
    return {
        **plan,
        "draft_id": draft_id,
        "expires_at": until,
        "entries": bundle["entries"],
        "changes": changes,
        "automatic_replies_enabled": False,
    }


def apply(conn, *, draft_id, session_id, actor_id):
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("缺少控制者身份")
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM knowledge_import_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if row is None or row["session_id"] != session_id:
            raise ValueError("知识导入草稿不可用")
        if row["applied_at"] is not None:
            if row["applied_by"] != actor_id:
                raise ValueError("导入操作者不匹配")
            return {**json.loads(row["result_json"]), "replayed": True}
        if parse_iso(row["expires_at"]) <= datetime.now(UTC) or row[
            "state_digest"
        ] != _state(conn):
            raise ValueError("知识或来源已变化，或草稿过期，请重新预览")
        bundle = validate_bundle(json.loads(row["bundle_json"]))
        result = _import_loaded_bundle(conn, bundle, bundle["bundle_digest"], actor_id)
        conn.execute(
            "UPDATE knowledge_import_drafts SET applied_by=?,applied_at=?,result_json=? WHERE draft_id=?",
            (actor_id, iso_now(), canonical_json(result), draft_id),
        )
    return {**result, "replayed": False}
