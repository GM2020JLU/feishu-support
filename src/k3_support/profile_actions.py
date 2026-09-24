"""Explicit owner corrections to existing profiles, with atomic stale checks."""

import uuid

from .ids import canonical_json, digest
from .routing import FUNCTION_ROLES, RELATIONSHIPS, set_requester_profile
from .timeutil import iso_now


class _Replay(Exception):
    pass


def apply(
    conn,
    *,
    requester_id,
    content_digest,
    relationship,
    function_role,
    reason,
    actor_id,
    request_id,
    reset_auto=False,
):
    if type(reset_auto) is not bool or (
        reset_auto and (relationship != "unknown" or function_role != "unknown")
    ):
        raise ValueError("撤销人工覆盖必须清空关系与职责")
    if (
        not isinstance(requester_id, str)
        or not 1 <= len(requester_id) <= 512
        or not isinstance(content_digest, str)
    ):
        raise ValueError("需要有效的同事 ID 和已查看的版本")
    if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
        raise ValueError("无效的请求编号")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("缺少控制者身份")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
        raise ValueError("请填写 1–1000 字符的人工核实依据")
    if (
        not isinstance(relationship, str)
        or relationship not in RELATIONSHIPS
        or not isinstance(function_role, str)
        or function_role not in FUNCTION_ROLES
    ):
        raise ValueError("无效的角色")
    proposal = canonical_json(
        {
            "relationship": relationship,
            "function_role": function_role,
            "reason": reason.strip(),
            **({"reset_auto": True} if reset_auto else {}),
        }
    )
    binding = (requester_id, content_digest, proposal, actor_id)

    def before_write():
        prior = conn.execute(
            "SELECT requester_id,content_digest,proposal_json,actor_id FROM profile_actions WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if prior is not None:
            if tuple(prior) != binding:
                raise ValueError("请求编号已用于其他修改")
            raise _Replay()
        live = conn.execute(
            "SELECT * FROM requester_profiles WHERE requester_id=?", (requester_id,)
        ).fetchone()
        if live is None or digest(dict(live)) != content_digest:
            raise ValueError("角色资料已变化，请重新查询和审核")
        if reset_auto and live["source"] != "operator":
            raise ValueError("当前没有人工覆盖需要撤销")
        conn.execute(
            "INSERT INTO profile_actions VALUES(?,?,?,?,?,?,?)",
            (
                request_id,
                requester_id,
                content_digest,
                proposal,
                canonical_json(dict(live)),
                actor_id,
                iso_now(),
            ),
        )

    old = conn.execute(
        "SELECT * FROM requester_profiles WHERE requester_id=?", (requester_id,)
    ).fetchone()
    if old is None:
        raise ValueError("只能修改已查询的缓存资料")
    try:
        set_requester_profile(
            conn,
            requester_id=requester_id,
            relationship=relationship,
            function_role=function_role,
            source="unknown" if reset_auto else "operator",
            relationship_confidence=0.0 if relationship == "unknown" else 1.0,
            function_confidence=0.0 if function_role == "unknown" else 1.0,
            display_name=old["display_name"],
            department=None if reset_auto else old["department"],
            job_title=None if reset_auto else old["job_title"],
            evidence={
                "operator": actor_id,
                "reason": reason.strip(),
            },
            verified_at=None if reset_auto else iso_now(),
            _before_write=before_write,
        )
    except _Replay:
        return {"requester_id": requester_id, "replayed": True}
    return {"requester_id": requester_id, "replayed": False}
