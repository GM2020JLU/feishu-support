"""Compact freshness bindings for owner-only Case actions, not authorization.

Control authenticates the operator separately, then checks this digest in its
write transaction. New turns can reuse fence=0, so fence alone is insufficient.
"""

from __future__ import annotations

import base64

from .ids import digest


def action_binding(conn, *, case_id: str, action: str) -> dict:
    if action not in {"resolve", "reopen", "claim", "delegate", "suggest_only"}:
        raise ValueError("unsupported Case lifecycle action")
    case = conn.execute(
        "SELECT case_id,version,lifecycle_round,state,owner FROM cases WHERE case_id=?",
        (case_id,),
    ).fetchone()
    if case is None:
        raise ValueError("Case not found")
    from .content_retirement import require_case_content, ContentRetiredError
    try:
        require_case_content(conn, case_id=case_id, lifecycle_round=case['lifecycle_round'])
    except ContentRetiredError:
        if action != 'reopen':
            raise
        # Reopen creates a new human-owned round. Its token must not depend on
        # removed source content, and cannot equal a pre-retirement token.
        receipt = conn.execute('SELECT receipt_id,preview_digest FROM case_content_retirements WHERE case_id=? AND lifecycle_round=?',
                               (case_id, case['lifecycle_round'])).fetchone()
        value = {'format':3, 'action':action, 'case':dict(case), 'retirement':dict(receipt)}
        token = base64.urlsafe_b64encode(bytes.fromhex(digest(value))[:16]).decode().rstrip('=')
        return {'token':token, 'version':case['version'], 'round':case['lifecycle_round'],
                'fence':0, 'turn_id':None}
    turn = conn.execute(
        """SELECT turn_id,source_event_pk,revision,fence,state,communication_owner,
                  communication_mode FROM conversation_turns WHERE case_id=?
           ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        (case_id,),
    ).fetchone()
    from .content_retirement import require_current_turn_after_retirement
    try:
        require_current_turn_after_retirement(conn, case_id=case_id,
            lifecycle_round=case['lifecycle_round'], turn_id=turn['turn_id'] if turn else None)
    except ContentRetiredError:
        if action in {'claim', 'delegate', 'suggest_only'}:
            raise
        # Lifecycle metadata must not pull the old turn's source body back in.
        turn = None
    event = conn.execute(
        """SELECT event_pk,payload_json,sender_id,chat_id,thread_id FROM inbound_events
           WHERE event_pk=?""", (turn["source_event_pk"] if turn else None,),
    ).fetchone() if turn is not None else None
    binding = {"format": 2, "action": action, "case": dict(case),
               "turn": dict(turn) if turn else None, "input": dict(event) if event else None}
    token = base64.urlsafe_b64encode(bytes.fromhex(digest(binding))[:16]).decode().rstrip("=")
    return {"token": token, "version": case["version"], "round": case["lifecycle_round"],
            "fence": turn["fence"] if turn else 0, "turn_id": turn["turn_id"] if turn else None}
