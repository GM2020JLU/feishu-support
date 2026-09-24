"""Read-only feedback target: an exact delivered reply, not latest Case knowledge."""

import json

from .ids import digest


def preview(conn, *, case_id, use_id, expected_digest=None):
    conn.execute('SAVEPOINT knowledge_use_preview')
    try:
        return _preview(conn, case_id=case_id, use_id=use_id, expected_digest=expected_digest)
    finally:
        conn.execute('RELEASE knowledge_use_preview')


def _preview(conn, *, case_id, use_id, expected_digest=None):
    from .content_retirement import require_case_content
    identity = conn.execute('''SELECT o.lifecycle_round FROM knowledge_uses u
        JOIN outbox o USING(outbox_id) WHERE u.case_id=? AND u.use_id=? AND o.case_id=?''',
        (case_id, use_id, case_id)).fetchone()
    if identity is None:
        raise ValueError('缺少对应的已发送知识回复')
    require_case_content(conn, case_id=case_id, lifecycle_round=identity['lifecycle_round'])
    row = conn.execute(
        """SELECT u.use_id,u.knowledge_id,u.case_id,u.outbox_id,
                  o.state,o.remote_message_id,o.payload_json,o.case_id AS sent_case_id
             FROM knowledge_uses u JOIN outbox o USING(outbox_id)
            WHERE u.case_id=? AND u.use_id=?""", (case_id, use_id)
    ).fetchone()
    if row is None or row["sent_case_id"] != case_id or row["state"] != "delivered" or not row["remote_message_id"]:
        raise ValueError("缺少对应的已发送知识回复")
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError("已发送回复格式无效")  # noqa: TRY004 - persisted JSON fails the operator validation contract
    binding = payload.get("knowledge_release") or {}
    if not isinstance(binding, dict):
        binding = {}
    provenance = binding.get("provenance") or {}
    if not isinstance(provenance, dict):
        provenance = {}
    fingerprint = provenance.get("knowledge_entry_fingerprint")
    version_bound = (
        binding.get("knowledge_ids") == [row["knowledge_id"]]
        and isinstance(fingerprint, str) and len(fingerprint) == 64
        and all(char in "0123456789abcdef" for char in fingerprint)
        and isinstance(payload.get("text"), str)
        and binding.get("text_digest") == digest(payload["text"])
    )
    token = digest(dict(row))
    if expected_digest is not None and expected_digest != token:
        raise ValueError("已发送回复记录已变化，请重新查看")
    return {"case_id": case_id, "use_id": use_id, "knowledge_id": row["knowledge_id"],
            "outbox_id": row["outbox_id"], "content_digest": token,
            "sent_text": payload.get("text", ""),
            "entry_fingerprint": fingerprint if version_bound else None,
            "version_bound": version_bound, "read_only": True,
            "automatic_publication": False,
            "note": "这是实际发送内容，不是知识库当前版本；反馈不能证明现场问题已解决。"
                    if version_bound else "历史回复缺少完整版本绑定，只供查阅，不能用于版本化反馈。"}
