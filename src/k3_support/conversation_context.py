"""Durable IM admission/context stamps; no model or external calls.

Ingress updates the stamp before a slow worker runs.  A projection is not an
authority grant; human communication ownership and unresolved associations are
checked independently.  Canonical message duplicates do not advance the stamp.
"""

from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext

from .context_facts import CONTEXT_FACT_POLICY, project_facts
from .ids import canonical_json, digest, new_id
from .message_format import is_feishu_ai_message
from .timeutil import epoch_now, iso_now, parse_iso

IM_SOURCES = {"feishu_bot_im", "feishu_user_poll"}


class ContextError(ValueError):
    pass


@contextmanager
def atomic(conn):
    nested = conn.in_transaction
    name = new_id("context_sp")
    conn.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}" if nested else "ROLLBACK")
        if nested:
            conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}" if nested else "COMMIT")


@contextmanager
def binding_atomic(conn):
    """Internal association only: hold SQLite's write lock across guard rebind.

    This is deliberately not the generic projection/ingress transaction. No
    model callback or arbitrary user-selected operation belongs in this scope.
    """
    with atomic(conn):
        transition = getattr(conn, "context_transition", None)
        with transition() if transition is not None else nullcontext():
            yield


def _available(conn):
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='conversation_contexts' AND type='table'"
        ).fetchone()
        is not None
    )


def _item(event):
    return {**dict(event), "payload": json.loads(event["payload_json"])}


def _event_digest(event):
    return digest(
        {
            **{
                key: event[key]
                for key in (
                    "event_pk",
                    "source",
                    "identity",
                    "external_id",
                    "sender_id",
                    "chat_id",
                    "thread_id",
                    "occurred_at",
                )
            },
            "payload": json.loads(event["payload_json"]),
        }
    )


def _canonical(item):
    payload = item["payload"]
    # Transport identity, display names and app links are not message edits.
    return digest(
        {
            "id": item["external_id"],
            "chat": item.get("chat_id"),
            "sender": item.get("sender_id"),
            "type": payload.get("chat_type"),
            "content": str(payload.get("content") or ""),
            "deleted": bool(payload.get("deleted")),
        }
    )


def _aliases(item):
    payload = item["payload"]
    return {
        str(value)
        for value in (
            item.get("external_id"),
            item.get("thread_id"),
            payload.get("root_id"),
            payload.get("parent_id"),
            payload.get("reply_to"),
        )
        if value
    }


def _matches(conn, item):
    values = sorted(_aliases(item))
    if not values or not item.get("chat_id"):
        return []
    return [
        str(row[0])
        for row in conn.execute(
            f"SELECT DISTINCT a.context_id FROM conversation_anchor_aliases a JOIN conversation_contexts c USING(context_id) WHERE a.chat_id=? AND a.alias IN ({','.join('?' for _ in values)}) AND c.state<>'retired'",
            (item["chat_id"], *values),
        )
    ]


def _mention_ids(payload):
    result = set()
    for mention in payload.get("mentions") or []:
        if not isinstance(mention, dict):
            continue
        for value in (mention.get("id"), mention.get("open_id")):
            if isinstance(value, dict):
                value = (
                    value.get("open_id")
                    or value.get("user_id")
                    or value.get("union_id")
                )
            if value:
                result.add(str(value))
    return result


def event_is_allowed(conn, config, item):
    payload = item["payload"]
    if (
        item.get("source") not in IM_SOURCES
        or is_feishu_ai_message(str(payload.get("content") or ""))
        or payload.get("sender_type") not in {None, "user"}
    ):
        return False
    if (
        payload.get("deleted")
        and not conn.execute(
            "SELECT 1 FROM inbound_events WHERE idempotency_key=?",
            ("feishu_im:message:" + item["external_id"],),
        ).fetchone()
    ):
        return False
    if payload.get("chat_type") == "p2p":
        return True
    if (
        payload.get("chat_type") != "group"
        or item.get("chat_id") not in config.raw["scope"]["technical_chat_ids"]
    ):
        return False
    owner = config.raw["identity"].get("feishu_owner_open_id")
    return bool(
        (owner and str(owner) in _mention_ids(payload))
        or (_available(conn) and _matches(conn, item))
    )


def _new_context(conn, item):
    context_id = new_id("ctx")
    now = iso_now()
    conn.execute(
        """INSERT INTO conversation_contexts(context_id,chat_id,chat_type,requester_id,input_digest,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)""",
        (
            context_id,
            item["chat_id"],
            item["payload"].get("chat_type", "p2p"),
            item.get("sender_id"),
            digest({"context": context_id}),
            now,
            now,
        ),
    )
    return context_id


def _bump(conn, context_id, reason):
    row = conn.execute(
        "SELECT * FROM conversation_contexts WHERE context_id=?", (context_id,)
    ).fetchone()
    if row is None or row["state"] == "retired":
        raise ContextError("context is missing or retired")
    state = (
        "conflict"
        if json.loads(row["conflicts_json"])
        else "awaiting_relation"
        if json.loads(row["pending_associations_json"])
        else "dirty"
    )
    conn.execute(
        "UPDATE conversation_contexts SET revision=revision+1,input_digest=?,state=?,updated_at=? WHERE context_id=?",
        (
            digest({"previous": row["input_digest"], "change": reason}),
            state,
            iso_now(),
            context_id,
        ),
    )
    # Never change started attempts or their eventual receipts. Their dispatch
    # check observes this stamp; an already-started operation cannot be recalled.
    conn.execute(
        """UPDATE outbox SET state='cancelled',suppression_reason='conversation_context_changed',lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                    WHERE channel='feishu_im' AND action_type IN ('reply','ack','clarify') AND state IN ('pending','retry')
                      AND (context_id=? OR (context_id IS NULL AND case_id=? AND lifecycle_round=?))""",
        (iso_now(), context_id, row["case_id"], row["lifecycle_round"]),
    )


def _conflict(conn, context_id, value):
    conflicts = json.loads(
        conn.execute(
            "SELECT conflicts_json FROM conversation_contexts WHERE context_id=?",
            (context_id,),
        ).fetchone()[0]
    )
    if value not in conflicts:
        conflicts.append(value)
        conn.execute(
            "UPDATE conversation_contexts SET conflicts_json=? WHERE context_id=?",
            (canonical_json(conflicts), context_id),
        )
        _bump(conn, context_id, {"conflict": value})


def _add_aliases(conn, context_id, item, event_pk):
    for alias in sorted(_aliases(item)):
        found = conn.execute(
            "SELECT context_id FROM conversation_anchor_aliases WHERE chat_id=? AND alias=?",
            (item["chat_id"], alias),
        ).fetchone()
        if found and found[0] != context_id:
            _conflict(
                conn,
                context_id,
                {"reason": "ambiguous_anchor", "other_context_id": found[0]},
            )
            _conflict(
                conn,
                found[0],
                {"reason": "ambiguous_anchor", "other_context_id": context_id},
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO conversation_anchor_aliases(chat_id,alias,context_id,source_event_pk) VALUES(?,?,?,?)",
                (item["chat_id"], alias, context_id, event_pk),
            )


def _member(conn, context_id, event_pk, item, *, role="colleague", relation="anchored"):
    previous = conn.execute(
        "SELECT * FROM conversation_context_members WHERE event_pk=?", (event_pk,)
    ).fetchone()
    canonical = _canonical(item)
    event = conn.execute(
        "SELECT * FROM inbound_events WHERE event_pk=?", (event_pk,)
    ).fetchone()
    stored_item = _item(event)
    coordinate_conflict = any(
        stored_item["payload"].get(key)
        and item["payload"].get(key)
        and stored_item["payload"][key] != item["payload"][key]
        for key in ("root_id", "reply_to", "parent_id")
    )
    old_thread, new_thread = stored_item.get("thread_id"), item.get("thread_id")
    if old_thread and new_thread and old_thread != new_thread:
        coordinate_conflict |= old_thread != item["payload"].get(
            "root_id"
        ) and new_thread != stored_item["payload"].get("root_id")
    if previous:
        if previous["context_id"] != context_id:
            raise ContextError("event is already associated with another context")
        if previous["canonical_digest"] != canonical or coordinate_conflict:
            _conflict(
                conn,
                context_id,
                {
                    "reason": "canonical_message_conflict",
                    "event_pk": event_pk,
                    "variant_digest": canonical,
                    "variant": item,
                },
            )
            return False
        _add_aliases(conn, context_id, item, event_pk)
        return False
    stored_canonical = _canonical(_item(event))
    sequence = conn.execute(
        "SELECT coalesce(max(member_sequence),0)+1 FROM conversation_context_members WHERE context_id=?",
        (context_id,),
    ).fetchone()[0]
    fingerprint = _event_digest(event)
    conn.execute(
        "INSERT INTO conversation_context_members VALUES(?,?,?,?,?,?,?,?)",
        (
            event_pk,
            context_id,
            sequence,
            fingerprint,
            stored_canonical,
            role,
            relation,
            iso_now(),
        ),
    )
    _add_aliases(conn, context_id, _item(event), event_pk)
    _bump(conn, context_id, {"event": event_pk, "digest": fingerprint, "role": role})
    if canonical != stored_canonical:
        _conflict(
            conn,
            context_id,
            {
                "reason": "canonical_message_conflict",
                "event_pk": event_pk,
                "variant_digest": canonical,
                "variant": item,
            },
        )
    return True


def _hold_candidates(conn, provisional_id, item, event_pk):
    candidates = [
        str(row[0])
        for row in conn.execute(
            """SELECT c.context_id FROM conversation_contexts c LEFT JOIN cases k ON k.case_id=c.case_id
           WHERE c.context_id<>? AND c.chat_id=? AND c.chat_type='p2p' AND c.requester_id=? AND c.state<>'retired'
             AND (c.case_id IS NULL OR k.state NOT IN ('resolved','cancelled','takeover'))""",
            (provisional_id, item["chat_id"], item.get("sender_id")),
        )
    ]
    if not candidates:
        return
    conn.execute(
        "UPDATE conversation_contexts SET candidate_context_ids_json=? WHERE context_id=?",
        (canonical_json(candidates), provisional_id),
    )
    for candidate in candidates:
        pending = json.loads(
            conn.execute(
                "SELECT pending_associations_json FROM conversation_contexts WHERE context_id=?",
                (candidate,),
            ).fetchone()[0]
        )
        pending[provisional_id] = {
            "event_pk": event_pk,
            "canonical_digest": _canonical(item),
            "reason": "unclassified_same_p2p_requester",
            "revoked_outbox_ids": [
                str(row[0])
                for row in conn.execute(
                    """SELECT o.outbox_id FROM outbox o JOIN conversation_contexts c ON c.context_id=?
                       WHERE o.channel='feishu_im' AND o.action_type IN ('reply','clarify','ack') AND o.state IN ('pending','retry')
                         AND (o.context_id=c.context_id OR (o.context_id IS NULL AND o.case_id=c.case_id AND o.lifecycle_round=c.lifecycle_round))
                       ORDER BY o.outbox_id""",
                    (candidate,),
                )
            ],
        }
        conn.execute(
            "UPDATE conversation_contexts SET pending_associations_json=? WHERE context_id=?",
            (canonical_json(pending), candidate),
        )
        _bump(
            conn,
            candidate,
            {"pending_association": provisional_id, "event_pk": event_pk},
        )


def admit_im_event(conn, config, item):
    """Admission, canonical event, membership and invalidation commit together."""
    from .store import ingest_event

    with atomic(conn):
        if not event_is_allowed(conn, config, item):
            return None, False
        matches = _matches(conn, item)
        owner = str(config.raw["identity"].get("feishu_owner_open_id") or "")
        is_owner = bool(owner and item.get("sender_id") == owner)
        owner_broadcast = (
            is_owner and not matches and item["payload"]["chat_type"] == "p2p"
        )
        if owner_broadcast:
            matches = [
                str(row[0])
                for row in conn.execute(
                    "SELECT context_id FROM conversation_contexts WHERE chat_id=? AND state<>'retired'",
                    (item["chat_id"],),
                )
            ]
        if is_owner and not matches:
            return None, False
        new_context = not matches
        previous = conn.execute(
            """SELECT m.context_id FROM inbound_events i JOIN conversation_context_members m USING(event_pk)
                                   WHERE i.idempotency_key=?""",
            ("feishu_im:message:" + item["external_id"],),
        ).fetchone()
        context_id = (
            previous[0]
            if previous
            else matches[0]
            if len(matches) == 1
            else _new_context(conn, item)
        )
        # Generic ingest only extends an already-known context. Suppress its
        # hook here: this admission owns the one member/revision transaction.
        event_pk, created = ingest_event(conn, **item, _context_hook=False)
        member_added = _member(
            conn,
            context_id,
            event_pk,
            item,
            role="owner" if is_owner else "colleague",
            relation="seed" if new_context else "anchored",
        )
        if len(matches) > 1 and not owner_broadcast:
            for match in matches:
                _conflict(
                    conn, match, {"reason": "ambiguous_anchor", "event_pk": event_pk}
                )
            _conflict(
                conn,
                context_id,
                {"reason": "ambiguous_anchor", "contexts": sorted(matches)},
            )
        if (
            new_context
            and member_added
            and item["payload"]["chat_type"] == "p2p"
            and not is_owner
        ):
            _hold_candidates(conn, context_id, item, event_pk)
        if is_owner:
            for target in {*matches, context_id}:
                _set_communication(
                    conn, target, "human", "silent", {"owner_event": event_pk}
                )
            conn.execute(
                "UPDATE inbound_events SET status='ignored' WHERE event_pk=? AND status='new'",
                (event_pk,),
            )
    return event_pk, created


def record_known_ingress(conn, event_pk, item):
    """No config means no new admission/anchor grant; only existing associations."""
    if not _available(conn) or item.get("source") not in IM_SOURCES:
        return
    row = conn.execute(
        "SELECT context_id FROM conversation_context_members WHERE event_pk=?",
        (event_pk,),
    ).fetchone()
    matches = [str(row[0])] if row else _matches(conn, item)
    if len(matches) == 1:
        _member(conn, matches[0], event_pk, item)
    elif len(matches) > 1:
        for context_id in matches:
            _conflict(
                conn, context_id, {"reason": "ambiguous_anchor", "event_pk": event_pk}
            )


def _records(conn, context_id):
    rows = conn.execute(
        """SELECT ie.*,m.event_digest,m.member_sequence,m.role FROM conversation_context_members m
                           JOIN inbound_events ie USING(event_pk) WHERE m.context_id=? ORDER BY julianday(ie.occurred_at),m.member_sequence""",
        (context_id,),
    ).fetchall()
    records = []
    changed = False
    for row in rows:
        changed |= _event_digest(row) != row["event_digest"]
        payload = json.loads(row["payload_json"])
        records.append(
            {
                "event_pk": row["event_pk"],
                "external_id": row["external_id"],
                "sender_id": row["sender_id"],
                "role": row["role"],
                "content": str(payload.get("content") or ""),
                "event_digest": row["event_digest"],
                "order": parse_iso(row["occurred_at"]).timestamp(),
                "member_sequence": row["member_sequence"],
            }
        )
    return records, changed


def context_snapshot(conn, context_id):
    conn.execute('SAVEPOINT context_content_snapshot')
    try:
        return _context_snapshot(conn, context_id)
    finally:
        conn.execute('RELEASE context_content_snapshot')


def _context_snapshot(conn, context_id):
    _require_context_content(conn, context_id)
    row = conn.execute(
        "SELECT * FROM conversation_contexts WHERE context_id=?", (context_id,)
    ).fetchone()
    if row is None:
        raise ContextError("context not found")
    result = dict(row)
    records, changed = _records(conn, context_id)
    result.update(
        query=result.pop("query_text"),
        facts=json.loads(result.pop("facts_json")),
        pending_associations=json.loads(result.pop("pending_associations_json")),
        candidate_context_ids=json.loads(result.pop("candidate_context_ids_json")),
        conflicts=json.loads(result.pop("conflicts_json")),
        source_changed=bool(changed),
        source_event_pks=[item["event_pk"] for item in records],
    )
    result["binding"] = {
        "context_id": context_id,
        "context_revision": result["revision"],
        "context_digest": result["input_digest"],
    }
    result["projection_changed"] = bool(
        result["projected_revision"] == result["revision"]
        and (
            result["facts_digest"] != digest(result["facts"])
            or (
                result["state"] == "ready"
                and result["facts"].get("query_digest") != digest(result["query"])
            )
        )
    )
    result["policy_changed"] = bool(
        result["state"] == "ready"
        and result["facts"].get("policy") != CONTEXT_FACT_POLICY
    )
    if changed or result["projection_changed"] or result["policy_changed"]:
        result["state"] = "conflict"
    return result


def context_fence(conn, context_id):
    """Constant-size persisted input/authority stamp, not a history projection."""
    row = conn.execute(
        """SELECT context_id,revision AS context_revision,input_digest AS context_digest,
                          communication_owner,communication_mode,state FROM conversation_contexts WHERE context_id=?""",
        (context_id,),
    ).fetchone()
    return dict(row) if row else None


def event_context_fence(conn, event_pk):
    if not _available(conn):
        return None
    row = conn.execute(
        "SELECT context_id FROM conversation_context_members WHERE event_pk=?",
        (event_pk,),
    ).fetchone()
    return context_fence(conn, row[0]) if row else None


def set_collection_state(conn, context_id, *, complete, cursor, observed_at):
    """Durable bounded backfill progress; a retry is not another input change."""
    with atomic(conn):
        row = conn.execute(
            "SELECT collection_complete FROM conversation_contexts WHERE context_id=?",
            (context_id,),
        ).fetchone()
        if row is None:
            raise ContextError("context not found")
        conn.execute(
            "UPDATE conversation_contexts SET collection_complete=?,thread_cursor=?,thread_poll_at=? WHERE context_id=?",
            (int(complete), cursor, observed_at, context_id),
        )
        if bool(row[0]) != complete:
            _bump(conn, context_id, {"collection_complete": complete})


def resolve_event_context(conn, event_pk):
    if not _available(conn):
        return None
    row = conn.execute(
        "SELECT context_id FROM conversation_context_members WHERE event_pk=?",
        (event_pk,),
    ).fetchone()
    return context_snapshot(conn, row[0]) if row else None


def _require_context_content(conn, context_id):
    from .content_retirement import require_case_content, ContentRetiredError
    identity = conn.execute('SELECT case_id,lifecycle_round FROM conversation_contexts WHERE context_id=?',
                            (context_id,)).fetchone()
    if identity is None:
        raise ContextError('context not found')
    require_case_content(conn, case_id=identity['case_id'], lifecycle_round=identity['lifecycle_round'])
    marker = conn.execute('''SELECT json_extract(facts_json,'$.schema') AS schema,
        json_extract(facts_json,'$.content_state') AS content_state
        FROM conversation_contexts WHERE context_id=?''', (context_id,)).fetchone()
    if marker['schema'] == 'retired-fact-content-v1' or marker['content_state'] == 'retired':
        # A partial/inconsistent write must not cause automatic reprojection
        # from still-present inbound bodies, even if its receipt is missing.
        raise ContentRetiredError('上下文含清理标记；禁止重建旧原文，请核对清理回执并提供新资料。')


def project_context(conn, context_id, expected_revision=None):
    snapshot = context_snapshot(conn, context_id)
    expected = snapshot["revision"] if expected_revision is None else expected_revision
    if snapshot["revision"] != expected or snapshot["state"] == "retired":
        raise ContextError("context projection is stale")
    records, changed = _records(conn, context_id)
    query = (
        records[0]["content"]
        if len(records) == 1
        else "\n\n".join(
            f"[消息 {item['external_id']} / {item['role']}]\n{item['content']}"
            for item in records
        )
    )
    incomplete = len(query) > 32768
    facts = (
        {
            "schema_version": 1,
            "incomplete_reason": "context_query_exceeds_32768",
            "query_length": len(query),
            "observed_scope": {},
            "fields": {},
            "mentions": [],
        }
        if incomplete
        else project_facts(records, query=query, requester_id=snapshot["requester_id"])
    )
    state = (
        "conflict"
        if changed or snapshot["conflicts"]
        else "awaiting_relation"
        if snapshot["pending_associations"]
        else "incomplete"
        if incomplete or not snapshot["collection_complete"]
        else "ready"
    )
    focus = next(
        (item["event_pk"] for item in reversed(records) if item["role"] == "colleague"),
        None,
    )
    with atomic(conn):
        if snapshot["policy_changed"]:
            current = conn.execute(
                "SELECT revision,input_digest FROM conversation_contexts WHERE context_id=?",
                (context_id,),
            ).fetchone()
            if tuple(current or ()) != (expected, snapshot["input_digest"]):
                raise ContextError("context changed while upgrading projection policy")
            _bump(conn, context_id, {"projection_policy": CONTEXT_FACT_POLICY})
            expected += 1
            snapshot["input_digest"] = conn.execute(
                "SELECT input_digest FROM conversation_contexts WHERE context_id=?", (context_id,)
            ).fetchone()[0]
        updated = conn.execute(
            """UPDATE conversation_contexts SET query_text=?,facts_json=?,facts_digest=?,focus_event_pk=?,state=?,projected_revision=?,updated_at=?
                                WHERE context_id=? AND revision=? AND input_digest=? AND state<>'retired'""",
            (
                "" if incomplete else query,
                canonical_json(facts),
                digest(facts),
                focus,
                state,
                expected,
                iso_now(),
                context_id,
                expected,
                snapshot["input_digest"],
            ),
        )
        if updated.rowcount != 1:
            raise ContextError("context changed while projecting")
    return context_snapshot(conn, context_id)


def validate_context_binding(conn, binding, *, require_ai=False):
    if not isinstance(binding, dict) or not binding.get("context_id"):
        return False, "context_binding_missing"
    from .content_retirement import ContentRetiredError
    try:
        snapshot = context_snapshot(conn, binding["context_id"])
    except ContentRetiredError:
        return False, 'context_content_retired'
    except (ContextError, TypeError, ValueError):
        return False, "context_binding_invalid"
    if snapshot["binding"] != {
        key: binding.get(key)
        for key in ("context_id", "context_revision", "context_digest")
    }:
        return False, "context_revision_changed"
    if snapshot["source_changed"]:
        return False, "context_source_changed"
    if snapshot["policy_changed"]:
        return False, "context_policy_changed"
    if snapshot["projection_changed"]:
        return False, "context_projection_changed"
    if (
        snapshot["state"] != "ready"
        or snapshot["revision"] != snapshot["projected_revision"]
    ):
        return False, "context_" + snapshot["state"]
    if require_ai and (
        snapshot["communication_owner"] != "ai"
        or snapshot["communication_mode"] != "respond"
    ):
        return False, "context_communication_revoked"
    return True, None


def _set_communication(conn, context_id, owner, mode, reason):
    row = conn.execute(
        "SELECT communication_owner,communication_mode FROM conversation_contexts WHERE context_id=?",
        (context_id,),
    ).fetchone()
    if row and tuple(row) != (owner, mode):
        conn.execute(
            "UPDATE conversation_contexts SET communication_owner=?,communication_mode=? WHERE context_id=?",
            (owner, mode, context_id),
        )
        _bump(conn, context_id, {"communication": [owner, mode], "reason": reason})


def set_case_communication(conn, case_id, *, owner, mode, reason):
    if owner not in {"ai", "human"} or mode not in {
        "respond",
        "silent",
        "suggest_only",
    }:
        raise ContextError("invalid context communication control")
    if not _available(conn):
        return
    for row in conn.execute(
        "SELECT context_id FROM conversation_contexts WHERE case_id=? AND state<>'retired'",
        (case_id,),
    ).fetchall():
        _set_communication(conn, row[0], owner, mode, reason)


def resolve_pending_associations(conn, provisional_context_id):
    """Call only after a routing decision binds the provisional context to a Case.

    Removing this hold does not revive any cancelled Outbox; old stamps stay old.
    """
    provisional = context_snapshot(conn, provisional_context_id)
    if provisional["case_id"] is None and not provisional["superseded_by"]:
        raise ContextError("pending association requires an explicit Case decision")
    selected_case = provisional["case_id"]
    if provisional["superseded_by"]:
        selected_case = conn.execute(
            "SELECT case_id FROM conversation_contexts WHERE context_id=?",
            (provisional["superseded_by"],),
        ).fetchone()[0]
    return _release_associations(conn, provisional, selected_case=selected_case)


def _release_associations(conn, provisional, *, selected_case):
    provisional_context_id = provisional["context_id"]
    released = []
    for target in provisional["candidate_context_ids"]:
        row = conn.execute(
            "SELECT pending_associations_json,state FROM conversation_contexts WHERE context_id=?",
            (target,),
        ).fetchone()
        if row is None or row["state"] == "retired":
            continue
        pending = json.loads(row[0])
        if provisional_context_id in pending:
            hold = pending.pop(provisional_context_id)
            conn.execute(
                "UPDATE conversation_contexts SET pending_associations_json=? WHERE context_id=?",
                (canonical_json(pending), target),
            )
            _bump(conn, target, {"association_decided": provisional_context_id})
            _require_context_recheck(
                conn,
                target,
                provisional_context_id,
                selected_case=selected_case,
                revoked_outbox_ids=hold.get("revoked_outbox_ids", []),
            )
            released.append(target)
    conn.execute(
        "UPDATE conversation_contexts SET candidate_context_ids_json='[]' WHERE context_id=?",
        (provisional_context_id,),
    )
    return released


def _require_context_recheck(
    conn, context_id, provisional_id, *, selected_case, revoked_outbox_ids
):
    current = context_snapshot(conn, context_id)
    if (
        not current["case_id"]
        or current["case_id"] == selected_case
        or current["communication_owner"] != "ai"
    ):
        return
    case = conn.execute(
        "SELECT * FROM cases WHERE case_id=?", (current["case_id"],)
    ).fetchone()
    if (
        case is None
        or case["state"] in {"resolved", "cancelled", "takeover", "paused"}
        or case["owner"] != "hermes"
        or case["lifecycle_round"] != current["lifecycle_round"]
    ):
        return
    revoked = [
        dict(row)
        for outbox_id in revoked_outbox_ids
        if (
            row := conn.execute(
                """SELECT outbox_id,action_type FROM outbox WHERE outbox_id=? AND case_id=? AND lifecycle_round=?
               AND state='cancelled' AND channel='feishu_im' AND action_type IN ('reply','clarify','ack')
               AND suppression_reason='conversation_context_changed'""",
                (outbox_id, case["case_id"], case["lifecycle_round"]),
            ).fetchone()
        )
        is not None
    ]
    if not revoked:
        return
    now = iso_now()
    # This is an automatic communication hold, not an operator action and not
    # a failed transport attempt. Independent execution ownership is untouched.
    conn.execute(
        """UPDATE conversation_turns SET communication_owner='human',communication_mode='silent',state='human_hold',fence=fence+1,updated_at=?
                    WHERE case_id=? AND source_event_pk IN (SELECT event_pk FROM conversation_context_members WHERE context_id=?)
                      AND communication_owner='ai' AND state IN ('open','ai_scheduled','ai_sending')""",
        (now, case["case_id"], context_id),
    )
    _set_communication(
        conn, context_id, "human", "silent", {"independent_topic": provisional_id}
    )
    fence = context_fence(conn, context_id)
    turns = [
        dict(row)
        for row in conn.execute(
            """SELECT turn_id,fence,state FROM conversation_turns WHERE case_id=?
               AND source_event_pk IN (SELECT event_pk FROM conversation_context_members WHERE context_id=?)
               AND state='human_hold' ORDER BY created_at DESC,rowid DESC""",
            (case["case_id"], context_id),
        )
    ]
    detail = {
        "reason": "independent_topic_recheck_required",
        "context_id": context_id,
        "context_binding": {
            key: fence[key]
            for key in ("context_id", "context_revision", "context_digest")
        },
        "lifecycle_round": case["lifecycle_round"],
        "turns": turns,
        "originating_context_id": provisional_id,
        "revoked_outbox_ids": [row["outbox_id"] for row in revoked],
        "notification_intent": True,
        "independent_execution_continues": True,
    }
    sequence = conn.execute(
        "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?",
        (case["case_id"],),
    ).fetchone()[0]
    conn.execute(
        """INSERT OR IGNORE INTO case_events(event_id,case_id,sequence,event_type,actor_type,actor_id,before_state,after_state,detail_json,idempotency_key,created_at,created_epoch)
                    VALUES(?,?,?,'context_recheck_required','system','context-association',?,?,?,?,?,?)""",
        (
            new_id("cev"),
            case["case_id"],
            sequence,
            case["state"],
            case["state"],
            canonical_json(detail),
            f"context-recheck:{context_id}:{provisional_id}",
            now,
            epoch_now(),
        ),
    )
    conn.execute(
        "UPDATE cases SET next_action=?,updated_at=?,updated_epoch=? WHERE case_id=?",
        (
            "新来信已确认是另一个问题；原自动回复已撤回。请重新委托查证或接管回复，独立调试任务继续。",
            now,
            epoch_now(),
            case["case_id"],
        ),
    )


def pending_context_rechecks(conn):
    """Read durable, still-current owner-notification intents, without enqueuing."""
    if not _available(conn):
        return []
    result = []
    for event in conn.execute(
        "SELECT * FROM case_events WHERE event_type='context_recheck_required' ORDER BY created_epoch,event_id"
    ):
        detail = json.loads(event["detail_json"])
        stamp = context_fence(conn, detail["context_id"])
        if (
            stamp is None
            or any(
                stamp.get(key) != value
                for key, value in detail["context_binding"].items()
            )
            or stamp["communication_owner"] != "human"
        ):
            continue
        case = conn.execute(
            "SELECT state,lifecycle_round FROM cases WHERE case_id=?",
            (event["case_id"],),
        ).fetchone()
        if (
            case is None
            or case["lifecycle_round"] != detail["lifecycle_round"]
            or case["state"] in {"resolved", "cancelled", "takeover"}
        ):
            continue
        result.append(
            {
                "event_id": event["event_id"],
                "case_id": event["case_id"],
                "created_at": event["created_at"],
                "idempotency_key": "context-recheck-notice:" + event["event_id"],
                **detail,
            }
        )
    return result


def release_independent_event(conn, event_pk):
    """Only a persisted, confident standalone ignore route can clear its holds."""
    with binding_atomic(conn):
        snapshot = resolve_event_context(conn, event_pk)
        route = conn.execute(
            "SELECT * FROM route_decisions WHERE event_pk=?", (event_pk,)
        ).fetchone()
        if (
            snapshot is None
            or snapshot["case_id"] is not None
            or snapshot["focus_event_pk"] != event_pk
            or not validate_context_binding(conn, snapshot["binding"])[0]
            or route is None
            or route["route"] != "ignore"
            or route["conversation_relation"] != "standalone"
            or route["confidence"] < 0.8
            or route["requires_owner_judgment"]
            or "non_work_noise" not in json.loads(route["reason_codes_json"])
        ):
            raise ContextError("independent ignore decision is not verified")
        released = _release_associations(conn, snapshot, selected_case=None)
        # Keep the source/aliases as evidence and allow a later explicitly
        # anchored follow-up to become a new work item. A duplicate event does
        # not recreate candidate holds, because admission requires a new member.
        return released


def bind_context_case(conn, context_id, case_id, round_number=None):
    with binding_atomic(conn):
        case = conn.execute(
            "SELECT lifecycle_round,owner,state FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if case is None or (round_number is not None and case[0] != round_number):
            raise ContextError("context Case round does not match")
        snapshot = context_snapshot(conn, context_id)
        if snapshot["case_id"] is not None and snapshot["case_id"] != case_id:
            raise ContextError("context cannot change its bound Case")
        target = conn.execute(
            "SELECT context_id FROM conversation_contexts WHERE case_id=? AND lifecycle_round=? AND state<>'retired'",
            (case_id, case[0]),
        ).fetchone()
        provisional_id = context_id
        if target and target[0] != context_id:
            target_id = str(target[0])
            target_snapshot = context_snapshot(conn, target_id)
            if (
                target_snapshot["chat_id"] != snapshot["chat_id"]
                or target_snapshot["chat_type"] != snapshot["chat_type"]
            ):
                raise ContextError("cannot merge contexts across conversations")
            sequence = conn.execute(
                "SELECT coalesce(max(member_sequence),0) FROM conversation_context_members WHERE context_id=?",
                (target_id,),
            ).fetchone()[0]
            for offset, member in enumerate(
                conn.execute(
                    "SELECT event_pk FROM conversation_context_members WHERE context_id=? ORDER BY member_sequence",
                    (context_id,),
                ).fetchall(),
                1,
            ):
                conn.execute(
                    "UPDATE conversation_context_members SET context_id=?,member_sequence=? WHERE event_pk=?",
                    (target_id, sequence + offset, member[0]),
                )
            conn.execute(
                "UPDATE conversation_anchor_aliases SET context_id=? WHERE context_id=?",
                (target_id, context_id),
            )
            conn.execute(
                "UPDATE conversation_contexts SET state='retired',superseded_by=? WHERE context_id=?",
                (target_id, context_id),
            )
            # Association never erases unresolved evidence or grants authority.
            conflicts = target_snapshot["conflicts"] + [
                value
                for value in snapshot["conflicts"]
                if value not in target_snapshot["conflicts"]
            ]
            pending = {
                **target_snapshot["pending_associations"],
                **snapshot["pending_associations"],
            }
            conn.execute(
                "UPDATE conversation_contexts SET conflicts_json=?,pending_associations_json=?,collection_complete=? WHERE context_id=?",
                (
                    canonical_json(conflicts),
                    canonical_json(pending),
                    int(
                        bool(
                            snapshot["collection_complete"]
                            and target_snapshot["collection_complete"]
                        )
                    ),
                    target_id,
                ),
            )
            _bump(
                conn,
                target_id,
                {
                    "merged_context": context_id,
                    "input_digest": snapshot["input_digest"],
                },
            )
            if snapshot["communication_owner"] == "human":
                _set_communication(
                    conn,
                    target_id,
                    "human",
                    snapshot["communication_mode"],
                    "merged_human_context",
                )
            context_id = target_id
        else:
            conn.execute(
                "UPDATE conversation_contexts SET case_id=?,lifecycle_round=? WHERE context_id=?",
                (case_id, case[0], context_id),
            )
        released = resolve_pending_associations(conn, provisional_id)
        if case[1] == "operator" or case[2] in {
            "paused",
            "takeover",
            "resolved",
            "cancelled",
        }:
            _set_communication(
                conn, context_id, "human", "silent", "Case_human_authority"
            )
    result = project_context(conn, context_id)
    result["released_context_ids"] = released
    return result


def adopt_case_event(conn, case_id, event_pk):
    """Associate already-authorized stored input, not an external collection API."""
    if not _available(conn):
        return None
    with binding_atomic(conn):
        existing = resolve_event_context(conn, event_pk)
        if existing:
            context_id = existing["context_id"]
        else:
            event = conn.execute(
                "SELECT * FROM inbound_events WHERE event_pk=?", (event_pk,)
            ).fetchone()
            if event is None or event["source"] not in IM_SOURCES:
                return None
            item = _item(event)
            matches = _matches(conn, item)
            if len(matches) > 1:
                raise ContextError("stored event has ambiguous context anchors")
            context_id = matches[0] if matches else _new_context(conn, item)
            _member(
                conn, context_id, event_pk, item, relation="explicit_case_association"
            )
        return bind_context_case(conn, context_id, case_id)
