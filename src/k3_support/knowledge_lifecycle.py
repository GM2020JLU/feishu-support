"""Content-bound operator withdrawal; never a knowledge publication shortcut."""

import uuid

from .knowledge import review
from .knowledge_preview import knowledge_preview
from .timeutil import iso_now


class _Replay(Exception):
    pass


def apply(conn, *, knowledge_id, content_digest, decision, actor_id, request_id):
    if decision not in ("candidate", "retired"):
        raise ValueError("此入口只允许退回待审核或停用，不能批准发布")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("缺少控制者身份")
    if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
        raise ValueError("操作请求编号不合法")
    if (
        not isinstance(knowledge_id, str)
        or not isinstance(content_digest, str)
        or len(content_digest) != 16
    ):
        raise ValueError("需要已查看的知识内容版本")
    binding = (knowledge_id, content_digest, decision, actor_id)

    def before_write():
        prior = conn.execute(
            "SELECT knowledge_id,content_digest,decision,actor_id FROM knowledge_lifecycle_actions WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if prior is not None:
            if tuple(prior) != binding:
                raise ValueError("请求编号已用于其他操作")
            raise _Replay()
        shown = knowledge_preview(
            conn, knowledge_id=knowledge_id, expected_digest=content_digest
        )
        conn.execute(
            "INSERT INTO knowledge_lifecycle_actions VALUES(?,?,?,?,?,?,?)",
            (
                request_id,
                knowledge_id,
                content_digest,
                decision,
                shown["knowledge"]["status"],
                actor_id,
                iso_now(),
            ),
        )

    replayed = False
    try:
        review(
            conn,
            knowledge_id=knowledge_id,
            reviewer_id=actor_id,
            decision=decision,
            _before_write=before_write,
        )
    except _Replay:
        replayed = True
    return {
        "knowledge_id": knowledge_id,
        "decision": decision,
        "replayed": replayed,
        "published": False,
    }
