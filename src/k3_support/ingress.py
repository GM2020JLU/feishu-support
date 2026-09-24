from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config
from .conversation_context import (
    admit_im_event,
    atomic,
    event_context_fence,
    event_is_allowed,
    set_collection_state,
)
from .coordination import record_operator_message, record_operator_reaction
from .db import transaction
from .ids import canonical_json, digest
from .lark import (
    CommandResult,
    LarkError,
    normalize_bot_event,
    normalize_mail_event,
    run_json,
    run_mail_json,
)
from .message_format import is_feishu_ai_message
from .store import ingest_event
from .timeutil import iso_now, parse_iso


class IngressError(RuntimeError):
    pass


def _messages(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("messages", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _ensure_complete_search(data: Any, label: str, *, meta=None) -> None:
    if not isinstance(data, dict):
        raise IngressError(f"{label} result is not an object")
    metadata = [value for value in (data.get("meta"), meta) if isinstance(value, dict)]
    incomplete = any(
        isinstance(value.get("pagination"), dict)
        and (
            value["pagination"].get("complete") is False
            or value["pagination"].get("next_token")
        )
        for value in metadata
    )
    if data.get("has_more") is True or incomplete:
        raise IngressError(f"{label} poll is incomplete")


def _mention_ids(mentions: Any) -> set[str]:
    values: set[str] = set()
    if not isinstance(mentions, list):
        return values
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        for raw in (mention.get("id"), mention.get("open_id")):
            if isinstance(raw, dict):
                raw = raw.get("open_id") or raw.get("user_id") or raw.get("union_id")
            if raw:
                values.add(str(raw))
    return values


def normalize_polled_message(value: dict[str, Any]) -> dict[str, Any]:
    message_id = value.get("message_id")
    chat_id = value.get("chat_id")
    create_time = str(value.get("create_time") or "")
    sender = value.get("sender") or {}
    if not isinstance(sender, dict):
        raise IngressError("polled message sender is not an object")
    sender_id = sender.get("id") or sender.get("open_id") or value.get("sender_id")
    if not all((message_id, chat_id, create_time, sender_id)):
        raise IngressError("polled message lacks immutable coordinates")
    if create_time.isdigit():
        raw_epoch = int(create_time)
        divisor = 1000 if raw_epoch > 10_000_000_000 else 1
        occurred = datetime.fromtimestamp(raw_epoch / divisor, UTC).isoformat()
    else:
        parsed_time = datetime.fromisoformat(create_time)
        if parsed_time.tzinfo is None:
            parsed_time = parsed_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        occurred = parsed_time.astimezone(UTC).isoformat()
    chat_type = value.get("chat_type")
    if chat_type not in {"p2p", "group"}:
        raise IngressError("polled message chat_type must be p2p or group")
    content = value.get("content")
    if isinstance(content, dict):
        content = content.get("text") or canonical_json(content)
    return {
        "source": "feishu_user_poll",
        "identity": "user",
        "external_id": str(message_id),
        "sender_id": str(sender_id),
        "chat_id": str(chat_id),
        "thread_id": value.get("thread_id") or value.get("root_id"),
        "occurred_at": occurred,
        "payload": {
            "chat_type": chat_type,
            "chat_name": value.get("chat_name"),
            "message_type": value.get("msg_type") or value.get("message_type"),
            "content": str(content or ""),
            "mentions": value.get("mentions") or [],
            "sender_type": sender.get("sender_type"),
            "sender_name": sender.get("name"),
            "deleted": bool(value.get("deleted")),
            "message_app_link": value.get("message_app_link"),
            "root_id": value.get("root_id"),
            "reply_to": value.get("reply_to") or value.get("parent_id"),
        },
    }


def _reaction_details(message: dict[str, Any]) -> list[dict[str, Any]]:
    reactions = message.get("reactions")
    if not isinstance(reactions, dict):
        return []
    flattened: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("reaction_id"):
                flattened.append(value)
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(reactions.get("details"))
    return flattened


def poll_operator_activity(
    conn: sqlite3.Connection,
    config: Config,
    *,
    now: datetime | None = None,
    runner: Callable[..., CommandResult] = run_json,
    chat_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Poll only chats with open turns; owner activity never enters the colleague inbox."""
    owner_id = config.raw["identity"].get("feishu_owner_open_id")
    if not owner_id:
        return {"state": "blocked_config", "recorded": 0, "chats": 0}
    observed = now or datetime.now(UTC)
    local_zone = ZoneInfo(config.raw["timezone"])
    where = "WHERE state IN ('open','ai_scheduled','ai_sending','human_hold')"
    args: tuple[str, ...] = ()
    if chat_ids is not None:
        if not chat_ids:
            return {"state": "ready", "recorded": 0, "chats": 0}
        ordered = tuple(sorted(chat_ids))
        where += " AND chat_id IN (" + ",".join("?" for _ in ordered) + ")"
        args = ordered
    chats = conn.execute(
        f"""SELECT chat_id,min(created_at) AS started_at FROM conversation_turns
              {where} GROUP BY chat_id""",
        args,
    ).fetchall()
    recorded = 0
    for chat in chats:
        started = parse_iso(chat["started_at"]) - timedelta(seconds=5)
        result = runner(
            [
                "im",
                "+chat-messages-list",
                "--chat-id",
                str(chat["chat_id"]),
                "--start",
                started.astimezone(local_zone).isoformat(timespec="seconds"),
                "--end",
                observed.astimezone(local_zone).isoformat(timespec="seconds"),
                "--order",
                "asc",
                "--page-all",
                "--as",
                "user",
            ]
        )
        data = result.data if isinstance(result.data, dict) else {}
        _ensure_complete_search(data, "operator activity", meta=result.meta)
        for raw in _messages(data):
            try:
                item = normalize_polled_message(raw)
            except (IngressError, ValueError):
                continue
            payload = item["payload"]
            if item["sender_id"] == owner_id and not payload["deleted"]:
                if _native_control_matches(config, item):
                    continue
                admit_im_event(conn, config, item)
                outcome = record_operator_message(
                    conn,
                    external_id=item["external_id"],
                    actor_id=str(owner_id),
                    chat_id=item["chat_id"],
                    occurred_at=item["occurred_at"],
                    content=payload["content"],
                    message_id=item["external_id"],
                    chat_type=str(payload.get("chat_type") or "p2p"),
                    thread_id=item.get("thread_id"),
                    root_message_id=payload.get("root_id"),
                    reply_to_message_id=payload.get("reply_to"),
                )
                recorded += int(bool(outcome.get("created")))
            for reaction in _reaction_details(raw):
                operator = reaction.get("operator") or {}
                emoji = reaction.get("emoji_type") or (
                    reaction.get("reaction_type") or {}
                ).get("emoji_type")
                if (
                    isinstance(operator, dict)
                    and operator.get("operator_id") == owner_id
                    and emoji == config.raw["coordination"]["claim_reaction"]
                    and reaction.get("reaction_id")
                ):
                    action_time = str(reaction.get("action_time") or "")
                    occurred_at = (
                        datetime.fromtimestamp(int(action_time) / 1000, UTC).isoformat()
                        if action_time.isdigit()
                        else observed.isoformat()
                    )
                    outcome = record_operator_reaction(
                        conn,
                        reaction_id=str(reaction["reaction_id"]),
                        actor_id=str(owner_id),
                        source_message_id=item["external_id"],
                        emoji_type=str(emoji),
                        occurred_at=occurred_at,
                    )
                    recorded += int(bool(outcome.get("created")))
    return {"state": "ready", "recorded": recorded, "chats": len(chats)}


def _watermark_start(
    conn: sqlite3.Connection, now: datetime, lookback_seconds: int
) -> datetime:
    row = conn.execute(
        "SELECT value_json FROM watermarks WHERE watermark_key='feishu_user_poll'"
    ).fetchone()
    if row is None:
        return now - timedelta(seconds=lookback_seconds)
    value = json.loads(row[0])
    return parse_iso(value["max_time"]) - timedelta(seconds=lookback_seconds)


def _mail_watermark_start(
    conn: sqlite3.Connection, now: datetime, lookback_seconds: int
) -> datetime:
    row = conn.execute(
        "SELECT value_json FROM watermarks WHERE watermark_key='feishu_mail_poll'"
    ).fetchone()
    if row is None:
        return now - timedelta(seconds=lookback_seconds)
    value = json.loads(row[0])
    return parse_iso(value["max_time"]) - timedelta(seconds=lookback_seconds)


def poll_anchored_threads(conn, config, *, now, runner, max_threads=4, max_pages=2):
    """Resume bounded, exact configured-group threads; never scan entire chats."""
    if (
        type(max_threads) is not int
        or not 1 <= max_threads <= 20
        or type(max_pages) is not int
        or not 1 <= max_pages <= 10
    ):
        raise ValueError("invalid thread poll budget")
    groups = tuple(config.raw["scope"]["technical_chat_ids"])
    if not groups:
        return {"ingested": 0, "threads": 0, "incomplete": 0}
    contexts = conn.execute(
        f"""SELECT c.* FROM conversation_contexts c LEFT JOIN cases k USING(case_id)
            WHERE c.chat_type='group' AND c.chat_id IN ({",".join("?" for _ in groups)}) AND c.state<>'retired'
              AND (k.case_id IS NULL OR k.state NOT IN ('resolved','cancelled','takeover'))
            ORDER BY c.thread_poll_at IS NOT NULL,c.thread_poll_at,c.created_at,c.context_id LIMIT ?""",
        (*groups, max_threads),
    ).fetchall()
    count = incomplete_count = 0
    for context in contexts:
        seed = conn.execute(
            """SELECT i.* FROM conversation_context_members m JOIN inbound_events i USING(event_pk)
                               WHERE m.context_id=? ORDER BY m.member_sequence LIMIT 1""",
            (context["context_id"],),
        ).fetchone()
        if seed is None:
            continue
        seed_payload = json.loads(seed["payload_json"])
        anchor = seed["thread_id"] or seed_payload.get("root_id") or seed["external_id"]
        if not isinstance(anchor, str) or not anchor.startswith(("om_", "omt_")):
            set_collection_state(
                conn,
                context["context_id"],
                complete=False,
                cursor=None,
                observed_at=now.isoformat(),
            )
            incomplete_count += 1
            continue
        argv = [
            "im",
            "+threads-messages-list",
            "--thread",
            anchor,
            "--order",
            "asc",
            "--page-size",
            "50",
            "--page-all",
            "--page-limit",
            str(max_pages),
            "--no-reactions",
            "--as",
            "user",
        ]
        if context["thread_cursor"]:
            argv.extend(["--page-token", context["thread_cursor"]])
        try:
            result = runner(argv)
        except Exception:
            set_collection_state(
                conn,
                context["context_id"],
                complete=False,
                cursor=context["thread_cursor"],
                observed_at=now.isoformat(),
            )
            raise
        data = result.data if isinstance(result.data, dict) else {}
        envelope_meta = getattr(result, "meta", None)
        meta = (
            envelope_meta
            if isinstance(envelope_meta, dict) and "pagination" in envelope_meta
            else data.get("meta", {})
        )
        pagination = meta.get("pagination", {}) if isinstance(meta, dict) else {}
        next_token = (
            pagination.get("next_token") if isinstance(pagination, dict) else None
        )
        complete = (
            isinstance(pagination, dict)
            and pagination.get("complete") is True
            and not data.get("has_more")
            and not next_token
        )
        # The already-returned page is one bounded write unit. In particular,
        # readers must never see a prefix as "complete" before the next item is
        # admitted or pagination incompleteness is made durable. No network or
        # model operation runs while holding this write lock.
        try:
            with atomic(conn):
                added, invalid = _admit_thread_messages(conn, config, context, data)
                count += added
                complete = complete and not invalid
                set_collection_state(
                    conn,
                    context["context_id"],
                    complete=complete,
                    cursor=None if complete or invalid else next_token,
                    observed_at=now.isoformat(),
                )
        except Exception:
            set_collection_state(
                conn,
                context["context_id"],
                complete=False,
                cursor=context["thread_cursor"],
                observed_at=now.isoformat(),
            )
            raise
        incomplete_count += int(not complete)
    return {"ingested": count, "threads": len(contexts), "incomplete": incomplete_count}


def _admit_thread_messages(conn, config, context, data):
    count, invalid = 0, False
    for raw in _messages(data):
        try:
            item = normalize_polled_message(raw)
        except (IngressError, ValueError):
            invalid = True
            continue
        if (
            item["chat_id"] != context["chat_id"]
            or item["payload"]["chat_type"] != "group"
        ):
            invalid = True
            continue
        # Explicit container response does not grant an unrelated @ message.
        aliases = {
            str(value)
            for value in (
                item["external_id"],
                item.get("thread_id"),
                item["payload"].get("root_id"),
                item["payload"].get("reply_to"),
            )
            if value
        }
        matched = conn.execute(
            f"SELECT 1 FROM conversation_anchor_aliases WHERE context_id=? AND chat_id=? AND alias IN ({','.join('?' for _ in aliases)})",
            (context["context_id"], context["chat_id"], *sorted(aliases)),
        ).fetchone()
        if not matched:
            invalid = True
            continue
        event_pk, created = admit_im_event(conn, config, item)
        count += int(created)
        if event_pk and item["sender_id"] == config.raw["identity"].get(
            "feishu_owner_open_id"
        ):
            record_operator_message(
                conn,
                external_id=item["external_id"],
                actor_id=item["sender_id"],
                chat_id=item["chat_id"],
                occurred_at=item["occurred_at"],
                content=item["payload"]["content"],
                message_id=item["external_id"],
                chat_type="group",
                thread_id=item.get("thread_id"),
                root_message_id=item["payload"].get("root_id"),
                reply_to_message_id=item["payload"].get("reply_to"),
            )
        if (
            event_pk
            and event_context_fence(conn, event_pk)["context_id"]
            != context["context_id"]
        ):
            invalid = True
    return count, invalid


def poll_user_messages(
    conn: sqlite3.Connection,
    config: Config,
    *,
    now: datetime | None = None,
    runner: Callable[..., CommandResult] = run_json,
) -> dict[str, Any]:
    observed = now or datetime.now(UTC)
    owner_id = config.raw["identity"].get("feishu_owner_open_id")
    if not owner_id:
        return {
            "state": "blocked_config",
            "reason": "feishu_owner_open_id missing",
            "ingested": 0,
        }
    start = _watermark_start(conn, observed, config.ingress("poll_lookback_seconds"))
    local_zone = ZoneInfo(config.raw["timezone"])
    time_args = [
        "--start",
        start.astimezone(local_zone).isoformat(timespec="seconds"),
        "--end",
        observed.astimezone(local_zone).isoformat(timespec="seconds"),
    ]
    p2p = runner(
        [
            "im",
            "+messages-search",
            "--query",
            "",
            "--chat-type",
            "p2p",
            *time_args,
            "--page-all",
            "--no-reactions",
            "--as",
            "user",
        ]
    )
    group = runner(
        [
            "im",
            "+messages-search",
            "--query",
            "",
            "--chat-type",
            "group",
            "--is-at-me",
            *time_args,
            "--page-all",
            "--no-reactions",
            "--as",
            "user",
        ]
    )
    _ensure_complete_search(p2p.data, "P2P message", meta=p2p.meta)
    _ensure_complete_search(group.data, "group message", meta=group.meta)
    allowed_groups = set(config.raw["scope"]["technical_chat_ids"])
    ingested = 0
    seen_at_max: list[str] = []
    max_time = start
    for raw in [*_messages(p2p.data), *_messages(group.data)]:
        try:
            item = normalize_polled_message(raw)
        except (IngressError, ValueError):
            continue
        payload = item["payload"]
        if payload.get("sender_type") not in {None, "user"} or is_feishu_ai_message(
            payload["content"]
        ):
            continue
        if item["sender_id"] == owner_id:
            if _native_feishu_control(conn, config, item):
                continue
            admit_im_event(conn, config, item)
            record_operator_message(
                conn,
                external_id=item["external_id"],
                actor_id=str(owner_id),
                chat_id=item["chat_id"],
                occurred_at=item["occurred_at"],
                content=payload["content"],
                message_id=item["external_id"],
                chat_type=str(payload.get("chat_type") or "p2p"),
                thread_id=item.get("thread_id"),
                root_message_id=payload.get("root_id"),
                reply_to_message_id=payload.get("reply_to"),
            )
            continue
        if payload["chat_type"] == "group":
            if item["chat_id"] not in allowed_groups:
                continue
            # The server-side --is-at-me filter is a retrieval hint, not an
            # authorization boundary. Verify the returned message itself.
            if not event_is_allowed(conn, config, item):
                continue
        _, created = admit_im_event(conn, config, item)
        ingested += int(created)
        occurred = parse_iso(item["occurred_at"])
        if occurred > max_time:
            max_time, seen_at_max = occurred, [item["external_id"]]
        elif occurred == max_time:
            seen_at_max.append(item["external_id"])
    threads = poll_anchored_threads(conn, config, now=observed, runner=runner)
    ingested += threads["ingested"]
    if threads["incomplete"]:
        raise IngressError(
            "anchored thread poll is incomplete; bounded cursor retained"
        )
    # Advance only after both searches and the bounded thread batch completed.
    with transaction(conn):
        conn.execute(
            """INSERT INTO watermarks(watermark_key,value_json,updated_at) VALUES('feishu_user_poll',?,?)
               ON CONFLICT(watermark_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
            (
                canonical_json(
                    {
                        "max_time": observed.isoformat(),
                        "message_ids_at_max": sorted(set(seen_at_max)),
                    }
                ),
                iso_now(),
            ),
        )
    operator = poll_operator_activity(conn, config, now=observed, runner=runner)
    return {
        "state": "ready",
        "ingested": ingested,
        "operator_activities": operator["recorded"],
        "operator_chats": operator["chats"],
        "window_start": start.isoformat(),
        "window_end": observed.isoformat(),
    }


def poll_user_mail(
    conn: sqlite3.Connection,
    config: Config,
    *,
    now: datetime | None = None,
    runner: Callable[..., CommandResult] = run_mail_json,
    max_pages: int = 4,
    should_stop: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Durably consume a bounded batch from one fixed provider pagination chain.

    The completed watermark is separate from the resumable cursor. A crash after
    any individual ingest can replay the page; it cannot skip its remaining IDs.
    Tokens are private opaque values, never reconstructed from message times.
    """
    if type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise ValueError("mail max_pages must be between 1 and 100")
    observed = now or datetime.now(UTC)
    if observed.tzinfo is None:
        raise ValueError("mail poll time must include timezone")
    observed = observed.astimezone(UTC).replace(microsecond=0)
    key = "feishu_mail_catchup"
    with transaction(conn):
        saved = conn.execute(
            "SELECT value_json FROM watermarks WHERE watermark_key=?", (key,)
        ).fetchone()
        if saved is None:
            start = _mail_watermark_start(
                conn, observed, config.ingress("poll_lookback_seconds")
            ).replace(microsecond=0)
            progress = {
                "version": 1,
                "window_start": start.isoformat(),
                "window_end": observed.isoformat(),
                "page_token": None,
                "seen_tokens": [],
                "seen_pages": [],
                "seen_ids": [],
                "pages": 0,
                "replays": 0,
            }
        else:
            progress = json.loads(saved[0])
            if progress.get("version") != 1:
                raise IngressError("mail cursor has an unsupported version")
        start, end = (
            parse_iso(progress["window_start"]),
            parse_iso(progress["window_end"]),
        )
        local_zone = ZoneInfo(config.raw["timezone"])
        time_range = {
            "folder": "inbox",
            "time_range": {
                "start_time": start.astimezone(local_zone).isoformat(
                    timespec="seconds"
                ),
                "end_time": end.astimezone(local_zone).isoformat(timespec="seconds"),
            },
        }
        binding = digest(
            {
                "mailbox": "me",
                "identity": "user",
                "filter": time_range,
                "max": 400,
                "owner": config.raw["identity"].get("feishu_owner_open_id"),
                "instance": str(config.path.absolute()),
                "provider_command": config.runtime("lark_cli_command"),
            }
        )
        if saved is not None and progress.get("binding") != binding:
            raise IngressError(
                "mail cursor instance/query binding changed; review the fixed window before replay"
            )
        progress["binding"] = binding
        expected = canonical_json(progress)
        if saved is None:
            conn.execute(
                "INSERT INTO watermarks(watermark_key,value_json,updated_at) VALUES(?,?,?)",
                (key, expected, iso_now()),
            )
        else:
            expected = saved[0]

    ingested = 0
    listed_count = 0

    def result(state: str, **extra: Any) -> dict[str, Any]:
        return {
            "state": state,
            "ingested": ingested,
            "listed": listed_count,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "pages": progress["pages"],
            "cursor_present": bool(progress["page_token"]),
            **extra,
        }

    for _ in range(max_pages):
        if should_stop():
            return result("stopped", complete=False)
        token = progress["page_token"]
        argv = [
            "mail",
            "+triage",
            "--filter",
            canonical_json(time_range),
            "--mailbox",
            "me",
            "--max",
            "400",
            "--format",
            "json",
            "--as",
            "user",
        ]
        if token:
            argv.extend(("--page-token", token))
        try:
            listed = runner(argv)
        except LarkError as exc:
            if should_stop():
                return result("stopped", complete=False)
            if token and exc.subtype in {
                "invalid_page_token",
                "expired_page_token",
                "page_token_expired",
                "page_token_invalid",
            }:
                # Provider tokens are not durable forever. Replay this exact
                # window explicitly, never jump to the latest clock time.
                progress.update(
                    {
                        "page_token": None,
                        "seen_tokens": [],
                        "seen_pages": [],
                        "seen_ids": [],
                        "pages": 0,
                        "replays": progress["replays"] + 1,
                    }
                )
                with transaction(conn):
                    changed = conn.execute(
                        "UPDATE watermarks SET value_json=?,updated_at=? WHERE watermark_key=? AND value_json=?",
                        (canonical_json(progress), iso_now(), key, expected),
                    ).rowcount
                    if changed != 1:
                        raise IngressError("mail cursor changed concurrently") from exc
                return result(
                    "degraded",
                    complete=False,
                    reason="expired_cursor_replaying_fixed_window",
                )
            raise
        if should_stop():
            return result("stopped", complete=False)
        data = listed.data
        from .pagination import PaginationError, decode_page
        try:
            page = decode_page(data, meta=listed.meta, current_token=token)
        except PaginationError as error:
            raise IngressError('mail triage ' + str(error)) from error
        summaries = page.messages
        message_ids = sorted({item["message_id"] for item in summaries})
        if len(message_ids) != len(summaries):
            raise IngressError("mail triage page repeats message IDs")
        page_digest = digest(message_ids)
        if page_digest in progress["seen_pages"]:
            raise IngressError(
                "mail triage repeated or reordered an already visited page"
            )
        if set(message_ids).intersection(progress["seen_ids"]):
            raise IngressError("mail triage page overlaps an already visited page")
        more = page.has_more
        next_token = page.next_token
        if more:
            if (
                not message_ids
                or not isinstance(next_token, str)
                or not next_token.startswith(("search:", "list:"))
                or not next_token.partition(":")[2]
            ):
                raise IngressError(
                    "mail triage has_more without a usable page token or page"
                )
            if token and next_token.partition(":")[0] != token.partition(":")[0]:
                raise IngressError("mail triage changed pagination token provider")
            if next_token == token or digest(next_token) in progress["seen_tokens"]:
                raise IngressError("mail triage pagination token repeated")
        known: set[str] = set()
        if message_ids:
            placeholders = ",".join("?" for _ in message_ids)
            known = {
                str(row[0]).removesuffix(":received")
                for row in conn.execute(
                    f"SELECT external_id FROM inbound_events WHERE source='feishu_mail' "
                    f"AND external_id IN ({placeholders})",
                    [f"{message_id}:received" for message_id in message_ids],
                )
            }
        new_ids = [message_id for message_id in message_ids if message_id not in known]
        items = []
        if new_ids:
            details = runner(
                [
                    "mail",
                    "+messages",
                    "--message-ids",
                    ",".join(new_ids),
                    "--mailbox",
                    "me",
                    "--html=false",
                    "--format",
                    "json",
                    "--as",
                    "user",
                ]
            )
            detail_data = details.data if isinstance(details.data, dict) else {}
            messages = detail_data.get("messages")
            if not isinstance(messages, list):
                raise IngressError("mail detail result has no messages list")
            returned_ids = [
                message.get("message_id")
                for message in messages
                if isinstance(message, dict)
            ]
            if (
                len(returned_ids) != len(new_ids)
                or set(returned_ids) != set(new_ids)
                or detail_data.get("unavailable_message_ids")
            ):
                raise IngressError(
                    "mail detail batch did not return every requested message"
                )
            items = [normalize_mail_event({"message": raw}) for raw in messages]
        for item in items:
            if should_stop():
                return result("stopped", complete=False)
            _, created = ingest_event(conn, **item)
            ingested += int(created)
        if should_stop():
            return result("stopped", complete=False)
        listed_count += len(message_ids)
        progress["pages"] += 1
        progress["seen_pages"].append(page_digest)
        progress["seen_ids"].extend(message_ids)
        if token:
            progress["seen_tokens"].append(digest(token))
        progress["page_token"] = next_token if more else None
        with transaction(conn):
            changed = conn.execute(
                "UPDATE watermarks SET value_json=?,updated_at=? WHERE watermark_key=? AND value_json=?",
                (canonical_json(progress), iso_now(), key, expected),
            ).rowcount
            if changed != 1:
                raise IngressError("mail cursor changed concurrently")
            if not more:
                conn.execute(
                    """INSERT INTO watermarks(watermark_key,value_json,updated_at)
                       VALUES('feishu_mail_poll',?,?) ON CONFLICT(watermark_key) DO UPDATE SET
                       value_json=excluded.value_json,updated_at=excluded.updated_at""",
                    (
                        canonical_json(
                            {"max_time": end.isoformat(), "message_ids": message_ids}
                        ),
                        iso_now(),
                    ),
                )
                conn.execute("DELETE FROM watermarks WHERE watermark_key=?", (key,))
        expected = canonical_json(progress)
        if not more:
            return result("ready", complete=True)
    return result("catching_up", complete=False)


def bot_event_allowed(config: Config, item: dict[str, Any], *, conn=None) -> bool:
    if conn is not None:
        return event_is_allowed(conn, config, item)
    payload = item["payload"]
    if is_feishu_ai_message(payload["content"]):
        return False
    if payload["chat_type"] == "p2p":
        return True
    if item["chat_id"] not in set(config.raw["scope"]["technical_chat_ids"]):
        return False
    owner_id = config.raw["identity"].get("feishu_owner_open_id")
    mention_ids = _mention_ids(payload.get("mentions"))
    return bool(owner_id and owner_id in mention_ids)


def _native_control_matches(config: Config, item: dict[str, Any]) -> bool:
    identity = config.raw["identity"]
    if (
        not identity.get("control_operator_id")
        or item["sender_id"] != identity.get("feishu_control_user_id")
        or item["chat_id"] != identity.get("feishu_control_chat_id")
        or item["payload"].get("message_type") != "text"
    ):
        return False
    from .hermes_plugin import _is_control_message

    return _is_control_message(item["payload"]["content"].strip())


def _native_feishu_control(conn: sqlite3.Connection, config: Config, item: dict[str, Any]) -> bool:
    """Keep an authenticated control message out of ordinary Case admission."""
    if not _native_control_matches(config, item):
        return False
    from .hermes_plugin import _format_receipt

    command = item["payload"]["content"].strip()
    # The native bot stream and user poll may observe the same message. Bind
    # the first immutable coordinates/content before executing any command.
    event_pk, _ = ingest_event(conn, **item, _context_hook=False)
    original = conn.execute(
        "SELECT sender_id,chat_id,payload_json FROM inbound_events WHERE event_pk=?",
        (event_pk,),
    ).fetchone()
    if (
        original is None
        or original["sender_id"] != item["sender_id"]
        or original["chat_id"] != item["chat_id"]
        or json.loads(original["payload_json"]).get("content") != item["payload"]["content"]
    ):
        raise IngressError("Feishu control message identity or content changed")
    from .control import ControlMessage, execute_control
    from .store import enqueue_outbox

    message = ControlMessage(
        user_id=item["sender_id"],
        chat_id=item["chat_id"],
        message_id=item["external_id"],
        text=command,
    )
    try:
        result = execute_control(
            conn, config, message, control_channel="feishu", text_only=True
        )
    except ValueError as exc:
        receipt = (
            "⚠️ 控制命令被拒绝："
            + str(exc)[:180]
            + "。请核对网页工作台，不要盲目重发。"
        )
    except RuntimeError:
        receipt = "⚠️ 控制结果未确认；请核对网页工作台和操作记录，不要盲目重发。"
    else:
        receipt = _format_receipt(True, result)
    if len(receipt) > 8000:
        receipt = "控制命令已处理，但回执过长；请在网页工作台核对结果，不要据此重复操作。"
    enqueue_outbox(
        conn,
        channel="feishu_im",
        action_type="control_receipt",
        destination=item["chat_id"],
        payload={"text": receipt},
        idempotency_key=f"feishu-native-control:{item['external_id']}",
        source_event_pk=event_pk,
    )
    return True


def ingest_bot_value(
    conn: sqlite3.Connection, config: Config, value: dict[str, Any], *, control_only: bool = False
) -> tuple[str | None, bool]:
    item = normalize_bot_event(value)
    if _native_feishu_control(conn, config, item):
        return None, False
    if control_only:
        return None, False
    result = admit_im_event(conn, config, item)
    owner = config.raw["identity"].get("feishu_owner_open_id")
    payload = item["payload"]
    if (
        owner
        and item["sender_id"] == owner
        and not is_feishu_ai_message(payload["content"])
    ):
        record_operator_message(
            conn,
            external_id=item["external_id"],
            actor_id=str(owner),
            chat_id=item["chat_id"],
            occurred_at=item["occurred_at"],
            content=payload["content"],
            message_id=item["external_id"],
            chat_type=payload["chat_type"],
            thread_id=item.get("thread_id"),
            root_message_id=payload.get("root_id"),
            reply_to_message_id=payload.get("reply_to") or payload.get("parent_id"),
        )
    return result


def ingest_mail_value(
    conn: sqlite3.Connection, value: dict[str, Any]
) -> tuple[str, bool]:
    item = normalize_mail_event(value)
    return ingest_event(conn, **item)
