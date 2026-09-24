from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config
from .coordination import suppress_outbox, validate_outbox_fence
from .db import transaction
from .delivery_attempts import action_digest, owns_claim, record_outcome, start_attempt
from .delivery_recovery import record_blocked_delivery
from .ids import canonical_json, new_id
from .lark import CommandResult, LarkError, run_json, run_mail_json
from .message_format import format_feishu_notice_text
from .operator_notifications import NOTICE_ACTIONS
from .store import enqueue_outbox
from .timeutil import iso_now


class DeliveryError(RuntimeError):
    pass


class DeliverySuppressed(DeliveryError):
    """A newer human or turn decision fenced this external write."""


class DeliveryUncertain(DeliveryError):
    """The remote call may have succeeded but returned no durable receipt."""


@dataclass(frozen=True)
class DeliveryReceipt:
    remote_id: str | None
    result: dict[str, Any]


def _remote_id(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get("message_id") or data.get("event_id") or data.get("record_id")
    return str(value) if value else None


def _short_key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:40]


def _lark_content_args(payload: dict[str, Any]) -> list[str]:
    """Select an explicit Feishu content mode; keep old rows as plain text."""
    content_format = payload.get("format", "text")
    if content_format not in {"text", "markdown"}:
        raise DeliveryError("unsupported Lark content format")
    text = payload.get("text")
    if not isinstance(text, str):
        raise DeliveryError("Lark message text is missing")
    return ["--markdown" if content_format == "markdown" else "--text", text]


def claim_outbox(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    lease_seconds: int = 120,
    eligible: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any] | None:
    now = datetime.now(UTC)
    expires = (now + timedelta(seconds=lease_seconds)).isoformat()
    with transaction(conn):
        while True:
            rows = conn.execute(
                """SELECT * FROM outbox WHERE state IN ('pending','retry')
                   AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                   AND (not_before IS NULL OR not_before<=?) ORDER BY created_at""",
                (now.isoformat(), now.isoformat()),
            ).fetchall()
            row = next(
                (
                    candidate
                    for candidate in rows
                    if eligible is None or eligible(dict(candidate))
                ),
                None,
            )
            if row is None:
                return None
            valid, reason = validate_outbox_fence(conn, dict(row))
            if valid:
                break
            suppress_outbox(conn, outbox_id=row["outbox_id"], reason=str(reason))
        changed = conn.execute(
            """UPDATE outbox SET state='sending',lease_owner=?,lease_expires_at=?,
               attempt_count=attempt_count+1,updated_at=?,claim_token=?,dispatch_started_at=NULL
               WHERE outbox_id=? AND state IN ('pending','retry')""",
            (worker_id, expires, now.isoformat(), new_id("ocm"), row[0]),
        )
        if changed.rowcount != 1:
            return None
        if row["turn_id"]:
            conn.execute(
                """UPDATE conversation_turns SET state='ai_sending',updated_at=?
                   WHERE turn_id=? AND communication_owner='ai' AND communication_mode='respond'
                     AND revision=? AND fence=? AND state='ai_scheduled'""",
                (
                    now.isoformat(),
                    row["turn_id"],
                    row["turn_revision"],
                    row["communication_fence"],
                ),
            )
        claimed = dict(
            conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (row[0],)).fetchone()
        )
        conn.execute(
            """INSERT INTO outbox_attempts(claim_token,outbox_id,attempt_number,
                   worker_id,action_digest,claimed_at) VALUES(?,?,?,?,?,?)""",
            (
                claimed["claim_token"],
                claimed["outbox_id"],
                claimed["attempt_count"],
                worker_id,
                action_digest(claimed),
                now.isoformat(),
            ),
        )
        return claimed


def hermes_send(
    target: str, text: str, *, executable: str = "hermes"
) -> DeliveryReceipt:
    process = subprocess.run(
        [executable, "send", "--to", target, "--json", text],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise DeliveryError(f"Hermes delivery failed with exit {process.returncode}")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise DeliveryError("Hermes returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DeliveryError("Hermes returned an invalid result")
    remote_id = value.get("message_id") or value.get("id")
    return DeliveryReceipt(str(remote_id) if remote_id else None, value)


def hermes_send_buttons(
    target: str,
    text: str,
    buttons: list[dict[str, str]],
    parse_mode: str | None = None,
    *,
    executable: str = "hermes",
) -> DeliveryReceipt:
    argv = [
        executable,
        "k3-support-telegram",
        "send-buttons",
        "--to",
        target,
        "--text",
        text,
        "--buttons-json",
        canonical_json(buttons),
    ]
    if parse_mode is not None:
        argv.extend(("--parse-mode", parse_mode))
    process = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise DeliveryError(
            f"Hermes button delivery failed with exit {process.returncode}"
        )
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise DeliveryError("Hermes returned invalid button-delivery JSON") from exc
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise DeliveryError("Hermes button delivery was rejected")
    remote_id = value.get("message_id")
    return DeliveryReceipt(str(remote_id) if remote_id else None, value)


def _lark_deliver(
    row: dict[str, Any], payload: dict[str, Any], runner: Callable[..., CommandResult]
) -> DeliveryReceipt:
    channel = row["channel"]
    if channel == "feishu_im" and row["action_type"] in {"reply", "ack", "clarify"}:
        identity = payload.get("identity")
        if identity not in {"bot", "user"}:
            raise DeliveryError("reply identity is missing")
        result = runner(
            [
                "im",
                "+messages-reply",
                "--message-id",
                row["destination"],
                *_lark_content_args(payload),
                "--idempotency-key",
                _short_key(row["idempotency_key"]),
                "--as",
                identity,
            ]
        )
    elif channel == "feishu_im" and row["action_type"] in ({"send", "control_receipt"} | NOTICE_ACTIONS):
        result = runner(
            [
                "im",
                "+messages-send",
                "--chat-id",
                row["destination"],
                *_lark_content_args(payload),
                "--idempotency-key",
                _short_key(row["idempotency_key"]),
                "--as",
                "bot",
            ]
        )
    elif channel in {"feishu_urgent_app", "feishu_urgent_sms"}:
        command = "urgent_app" if channel.endswith("app") else "urgent_sms"
        result = runner(
            [
                "im",
                "messages",
                command,
                "--message-id",
                row["destination"],
                "--user-id-type",
                "open_id",
                "--data",
                canonical_json({"user_id_list": payload["user_id_list"]}),
                "--as",
                "bot",
            ]
        )
    else:
        raise DeliveryError(
            f"unsupported Lark delivery: {channel}/{row['action_type']}"
        )
    data = result.data if isinstance(result.data, dict) else {"data": result.data}
    invalid = data.get("invalid_user_id_list") if isinstance(data, dict) else None
    if invalid:
        raise DeliveryError("urgent delivery returned invalid user IDs")
    return DeliveryReceipt(_remote_id(data), data)


def _extract_message_app_link(data: Any, message_id: str) -> str | None:
    if isinstance(data, dict):
        direct = data.get("message_app_link")
        if isinstance(direct, str):
            return direct
        for key in ("messages", "items", "results"):
            values = data.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict):
                    continue
                if str(item.get("message_id") or "") != message_id:
                    continue
                value = item.get("message_app_link")
                if isinstance(value, str):
                    return value
    return None


def _mail_deliver(
    row: dict[str, Any],
    payload: dict[str, Any],
    *,
    lark_runner: Callable[..., CommandResult],
    mail_runner: Callable[..., CommandResult],
) -> DeliveryReceipt:
    if row["action_type"] == "share_to_owner":
        message_id = payload.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise DeliveryError("mail share message_id is missing")
        result = mail_runner(
            [
                "mail",
                "+share-to-chat",
                "--message-id",
                message_id,
                "--receive-id",
                row["destination"],
                "--receive-id-type",
                "chat_id",
                "--format",
                "json",
                "--as",
                "user",
            ]
        )
        data = result.data if isinstance(result.data, dict) else {"data": result.data}
        im_message_id = data.get("im_message_id") if isinstance(data, dict) else None
        if not im_message_id:
            raise DeliveryUncertain("mail share returned no IM message ID")
        return DeliveryReceipt(str(im_message_id), data)
    if row["action_type"] == "resolve_app_link":
        im_message_id = str(row["destination"])
        result = lark_runner(
            [
                "im",
                "+messages-mget",
                "--message-ids",
                im_message_id,
                "--no-reactions",
                "--as",
                "user",
            ]
        )
        data = result.data if isinstance(result.data, dict) else {"data": result.data}
        app_link = _extract_message_app_link(data, im_message_id)
        if not app_link or not app_link.startswith("https://"):
            raise LarkError(
                "shared mail message has no HTTPS AppLink", error_type="protocol"
            )
        return DeliveryReceipt(
            im_message_id,
            {"im_message_id": im_message_id, "message_app_link": app_link},
        )
    raise DeliveryError(f"unsupported Mail delivery: {row['action_type']}")


def _feature_allowed(
    conn: sqlite3.Connection,
    config: Config,
    row: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    from .runtime_control import outbox_eligible

    if not outbox_eligible(conn, config, row):
        return False
    if config.mode != "active":
        return False
    if row["channel"] == "mail" or row["action_type"] == "mail_summary":
        return config.feature("mail")
    if row["channel"] == "feishu_im" and row["action_type"] == "reply":
        if payload.get("reply_basis") == "verified_evidence":
            return config.feature("codex")
        if payload.get("reply_basis") == "verified_link_route":
            return config.feature("auto_faq")
        return config.feature("auto_faq")
    return True


def _content_eligibility(conn, config, row, payload) -> tuple[bool, str | None]:
    """Recheck the exact content's evidence, distinct from authority fencing."""
    from .content_retirement import ContentRetiredError, require_case_content
    try:
        require_case_content(conn, case_id=row['case_id'], lifecycle_round=row['lifecycle_round'])
    except ContentRetiredError:
        return False, 'case_content_retired'
    if row["action_type"] == "owner_digest":
        from .notification_digest import valid

        if not valid(conn, row):
            return False, "ordinary_digest_changed"
    if row["channel"] == "feishu_im" and row["action_type"] == "clarify":
        from .clarification_context import validate_clarification_delivery

        if not validate_clarification_delivery(conn, item=row):
            return (
                False,
                "clarification_review: current question is no longer independently justified",
            )
    from .knowledge_release import verify_knowledge_reply

    release = verify_knowledge_reply(conn, config, row=row, payload=payload)
    if not release["ready"]:
        return False, "knowledge_release: " + str(release["reason"])
    return True, None


def _begin_dispatch(
    conn: sqlite3.Connection,
    config: Config,
    row: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """Linearize dispatch against revoke/reclaim; never lock around transport."""
    rejection = None
    with transaction(conn):
        if not owns_claim(conn, row):
            raise DeliverySuppressed("external write suppressed: outbox claim lost")
        started = conn.execute(
            "SELECT dispatch_started_at FROM outbox_attempts WHERE claim_token=?",
            (row["claim_token"],),
        ).fetchone()
        if started is not None and started[0] is not None:
            raise DeliverySuppressed(
                "external write suppressed: outbox attempt already dispatched"
            )
        # Feature evaluation materializes timed-mode expiry and may advance the
        # global fence. Never use a fence verdict taken before that transition.
        allowed = _feature_allowed(conn, config, row, payload)
        valid, reason = validate_outbox_fence(conn, row)
        if valid and allowed:
            valid, reason = _content_eligibility(conn, config, row, payload)
        if not valid:
            rejection = str(reason)
            suppress_outbox(conn, outbox_id=row["outbox_id"], reason=rejection)
            record_outcome(
                conn, row, event_type="suppressed", detail={"reason": rejection}
            )
            record_blocked_delivery(conn, config, row, reason=rejection)
        elif not allowed:
            rejection = "delivery disabled before dispatch"
            conn.execute(
                """UPDATE outbox SET state='pending',lease_owner=NULL,lease_expires_at=NULL,
                   updated_at=? WHERE outbox_id=? AND claim_token=? AND state='sending'""",
                (iso_now(), row["outbox_id"], row["claim_token"]),
            )
        elif not start_attempt(conn, row):
            rejection = "outbox attempt already dispatched"
    if rejection:
        raise DeliverySuppressed(f"external write suppressed: {rejection}")


def deliver_claimed(
    conn: sqlite3.Connection,
    config: Config,
    row: dict[str, Any],
    *,
    lark_runner: Callable[..., CommandResult] = run_json,
    mail_runner: Callable[..., CommandResult] = run_mail_json,
    telegram_runner: Callable[[str, str], DeliveryReceipt] = hermes_send,
    telegram_button_runner: Callable[..., DeliveryReceipt] = hermes_send_buttons,
) -> DeliveryReceipt:
    if row["state"] != "sending":
        raise DeliveryError("outbox row must be claimed first")
    payload = json.loads(row["payload_json"])
    with transaction(conn):
        if not owns_claim(conn, row):
            raise DeliverySuppressed("external write suppressed: outbox claim lost")
        started = conn.execute(
            "SELECT dispatch_started_at FROM outbox_attempts WHERE claim_token=?",
            (row["claim_token"],),
        ).fetchone()
        if started is not None and started[0] is not None:
            raise DeliverySuppressed(
                "external write suppressed: outbox attempt already dispatched"
            )
        allowed = _feature_allowed(conn, config, row, payload)
        valid, reason = validate_outbox_fence(conn, row)
        if valid and allowed:
            valid, reason = _content_eligibility(conn, config, row, payload)
        if not valid:
            suppress_outbox(conn, outbox_id=row["outbox_id"], reason=str(reason))
            record_outcome(
                conn, row, event_type="suppressed", detail={"reason": reason}
            )
            record_blocked_delivery(conn, config, row, reason=str(reason))
    if not valid:
        raise DeliverySuppressed(f"external write suppressed: {reason}")
    if not allowed:
        with transaction(conn):
            if not owns_claim(conn, row):
                raise DeliverySuppressed("external write suppressed: outbox claim lost")
            conn.execute(
                """UPDATE outbox SET state='pending',lease_owner=NULL,lease_expires_at=NULL,
                   updated_at=? WHERE outbox_id=? AND claim_token=? AND state='sending'""",
                (iso_now(), row["outbox_id"], row["claim_token"]),
            )
            if row.get("turn_id"):
                conn.execute(
                    """UPDATE conversation_turns SET state='ai_scheduled',updated_at=?
                       WHERE turn_id=? AND state='ai_sending' AND revision=? AND fence=?""",
                    (
                        iso_now(),
                        row["turn_id"],
                        row["turn_revision"],
                        row["communication_fence"],
                    ),
                )
        raise DeliveryError("delivery is disabled by mode or feature flag")
    dispatched = False
    try:
        if row["channel"] == "telegram":
            _begin_dispatch(conn, config, row, payload)
            dispatched = True
            if payload.get("buttons"):
                button_args = (row["destination"], payload["text"], payload["buttons"])
                if payload.get("parse_mode"):
                    receipt = telegram_button_runner(
                        *button_args, parse_mode=payload["parse_mode"]
                    )
                else:
                    receipt = telegram_button_runner(*button_args)
            else:
                receipt = telegram_runner(row["destination"], payload["text"])
        else:
            if (
                row.get("turn_id")
                and row["channel"] == "feishu_im"
                and row["action_type"] in {"reply", "ack", "clarify"}
            ):
                # A bounded, exact-chat read closes the gap between the periodic
                # poll and the external write. Any owner activity fences this row.
                from .ingress import poll_operator_activity

                turn = conn.execute(
                    "SELECT chat_id FROM conversation_turns WHERE turn_id=?",
                    (row["turn_id"],),
                ).fetchone()
                poll_operator_activity(
                    conn,
                    config,
                    now=datetime.now(UTC),
                    runner=lark_runner,
                    chat_ids={str(turn["chat_id"])} if turn else set(),
                )
            _begin_dispatch(conn, config, row, payload)
            dispatched = True
            receipt = (
                _mail_deliver(
                    row, payload, lark_runner=lark_runner, mail_runner=mail_runner
                )
                if row["channel"] == "mail"
                else _lark_deliver(row, payload, lark_runner)
            )
        if (
            row["channel"] in {"telegram", "feishu_im", "mail"}
            and not receipt.remote_id
        ):
            raise DeliveryUncertain(
                f"{row['channel']} delivery returned no remote message ID"
            )
    except DeliverySuppressed:
        raise
    except (
        Exception
    ) as exc:  # transport failure, including timeouts and invalid results
        retry_safe = row["channel"] == "feishu_im" or (
            row["channel"] == "mail" and row["action_type"] == "resolve_app_link"
        )
        uncertain = isinstance(exc, DeliveryUncertain) or (
            dispatched and not retry_safe
        )
        permanent = (
            uncertain
            or (
                isinstance(exc, LarkError)
                and exc.error_type in {"authorization", "authentication", "validation"}
            )
            or (row["channel"] == "mail" and row["action_type"] == "share_to_owner")
        )
        state = (
            "permanent_failure"
            if permanent or int(row["attempt_count"]) >= 8
            else "retry"
        )
        delay = min(300, 5 * (3 ** max(0, int(row["attempt_count"]) - 1)))
        next_attempt = (
            (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
            if state == "retry"
            else None
        )
        with transaction(conn):
            record_outcome(
                conn,
                row,
                event_type="uncertain" if uncertain else "failed",
                detail={"error_type": type(exc).__name__, "message": str(exc)},
            )
        with transaction(conn):
            # Audit a late failure, but do not change a successor or cancellation.
            if not owns_claim(conn, row):
                raise
            allowed = _feature_allowed(conn, config, row, payload)
            valid, reason = validate_outbox_fence(conn, row)
            if not valid or not allowed:
                state = "cancelled"
                next_attempt = None
                reason = str(reason or "mode_revoked_after_dispatch")
                record_outcome(
                    conn, row, event_type="suppressed", detail={"reason": reason}
                )
            conn.execute(
                """UPDATE outbox SET state=?,lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=?,
                   remote_result_json=?,updated_at=?,suppression_reason=coalesce(?,suppression_reason)
                   WHERE outbox_id=? AND claim_token=?
                   AND state='sending'""",
                (
                    state,
                    next_attempt,
                    canonical_json(
                        {"error_type": type(exc).__name__, "message": str(exc)}
                    ),
                    iso_now(),
                    reason,
                    row["outbox_id"],
                    row["claim_token"],
                ),
            )
            if state == "permanent_failure" and row["channel"] == "mail":
                from .mail import fail_mail_outbox

                fail_mail_outbox(
                    conn,
                    outbox_id=row["outbox_id"],
                    action_type=row["action_type"],
                    error=f"{type(exc).__name__}: {exc}",
                )
        raise

    with transaction(conn):
        record_outcome(
            conn,
            row,
            event_type="delivered",
            detail=receipt.result,
            remote_id=receipt.remote_id,
        )
    # Keep known external success even if a projection/summary/feedback update
    # fails. Reconcile can recover this exact attempt without sending again.
    with transaction(conn):
        delivered_at = iso_now()
        if not owns_claim(conn, row):
            # The receipt belongs to this attempt only, not the current projection.
            return receipt
        allowed = _feature_allowed(conn, config, row, payload)
        valid, reason = validate_outbox_fence(conn, row)
        if not valid or not allowed:
            reason = str(reason or "mode_revoked_after_dispatch")
            suppress_outbox(conn, outbox_id=row["outbox_id"], reason=reason)
            record_outcome(
                conn, row, event_type="suppressed", detail={"reason": reason}
            )
            return receipt
        conn.execute(
            """UPDATE outbox SET state='delivered',lease_owner=NULL,lease_expires_at=NULL,
               remote_message_id=?,remote_result_json=?,delivered_at=?,updated_at=? WHERE outbox_id=?
               AND claim_token=? AND state='sending'""",
            (
                receipt.remote_id,
                canonical_json(receipt.result),
                delivered_at,
                delivered_at,
                row["outbox_id"],
                row["claim_token"],
            ),
        )
        finalize_delivery_effects(conn, config, row, receipt, delivered_at=delivered_at)
    return receipt


def finalize_delivery_effects(
    conn: sqlite3.Connection,
    config: Config,
    row: dict[str, Any],
    receipt: DeliveryReceipt,
    *,
    delivered_at: str,
) -> None:
    """Idempotent business projections shared by normal completion and recovery.

    A durable receipt is historical evidence, not renewed communication rights.
    Revoked effects are finalized as suppressed so a later mode change cannot
    replay an old answer, grant authority or enqueue obsolete urgency actions.
    """
    with nullcontext(conn) if conn.in_transaction else transaction(conn):
        live = conn.execute(
            "SELECT * FROM outbox WHERE outbox_id=? AND claim_token=? AND state='delivered'",
            (row["outbox_id"], row["claim_token"]),
        ).fetchone()
        if live is None or live["effects_finalized_at"] is not None:
            return
        if row["action_type"] == "owner_digest":
            from .notification_digest import finalize

            # Historical receipt means this summary reached the owner even if
            # a mode changed meanwhile. Preserve newly urgent originals.
            finalize(conn, row)
            conn.execute(
                "UPDATE outbox SET effects_finalized_at=? WHERE outbox_id=? AND claim_token=?",
                (iso_now(), row["outbox_id"], row["claim_token"]),
            )
            return
        payload = json.loads(row["payload_json"])
        allowed = _feature_allowed(conn, config, row, payload)
        valid, reason = validate_outbox_fence(conn, row)
        if valid and allowed:
            valid, reason = _content_eligibility(conn, config, row, payload)
        if not valid or not allowed:
            record_outcome(
                conn,
                row,
                event_type="suppressed",
                detail={"reason": reason or "mode_revoked_before_finalization"},
            )
            record_blocked_delivery(
                conn,
                config,
                row,
                reason=str(reason or "mode_revoked_before_finalization"),
            )
            conn.execute(
                "UPDATE outbox SET effects_finalized_at=? WHERE outbox_id=? AND claim_token=?",
                (iso_now(), row["outbox_id"], row["claim_token"]),
            )
            return
        if row["channel"] == "mail":
            if not receipt.remote_id:
                raise DeliveryError("mail delivery has no remote message ID")
            from .mail import complete_mail_outbox_delivery

            complete_mail_outbox_delivery(
                conn,
                outbox_id=row["outbox_id"],
                action_type=row["action_type"],
                remote_message_id=receipt.remote_id,
                result=receipt.result,
            )
        if row["action_type"] == "mail_summary":
            if not receipt.remote_id:
                raise DeliveryError("mail summary delivery has no remote message ID")
            from .mail import commit_summary_delivery

            commit_summary_delivery(
                conn,
                outbox_id=row["outbox_id"],
                remote_message_id=receipt.remote_id,
            )
        if row.get("turn_id"):
            next_turn_state = "ai_sent" if row["action_type"] == "reply" else "open"
            conn.execute(
                """UPDATE conversation_turns SET state=?,updated_at=? WHERE turn_id=?
                   AND communication_owner='ai' AND communication_mode='respond'
                   AND revision=? AND fence=?""",
                (
                    next_turn_state,
                    delivered_at,
                    row["turn_id"],
                    row["turn_revision"],
                    row["communication_fence"],
                ),
            )
        if (
            row["channel"] == "feishu_im"
            and row["action_type"] == "reply"
            and row.get("case_id")
        ):
            from .lifecycle import record_reply_delivered

            record_reply_delivered(
                conn,
                row=row,
                remote_message_id=receipt.remote_id,
                delivered_at=delivered_at,
            )
            from .knowledge import record_delivered_use

            record_delivered_use(
                conn, case_id=str(row["case_id"]), outbox_id=str(row["outbox_id"])
            )
        if (
            row["channel"] in {"telegram", "feishu_im"}
            and row["action_type"] == "incident_alert"
            and row.get("case_id")
        ):
            # A mail alert is complete only after its owner notification has a
            # receipt. Non-mail incident Cases simply match no rows here.
            conn.execute(
                "UPDATE mail_items SET notified=1,updated_at=? WHERE case_id=?",
                (delivered_at, row["case_id"]),
            )
        if (
            row["channel"] == "feishu_im"
            and row["action_type"] == "send"
            and payload.get("p0_owner_open_id")
        ):
            if not receipt.remote_id:
                raise DeliveryError(
                    "P0 bot alert has no remote message ID for urgent delivery"
                )
            urgent_channels = (
                ("feishu_urgent_app", "feishu_app_urgent"),
                ("feishu_urgent_sms", "feishu_sms_urgent"),
            )
            for channel, notification in urgent_channels:
                if not config.notification(notification):
                    continue
                enqueue_outbox(
                    conn,
                    channel=channel,
                    action_type="urgent",
                    destination=receipt.remote_id,
                    payload={"user_id_list": [payload["p0_owner_open_id"]]},
                    idempotency_key=f"{row['idempotency_key']}:{channel}",
                    case_id=row["case_id"],
                )
        conn.execute(
            "UPDATE outbox SET effects_finalized_at=? WHERE outbox_id=? AND claim_token=?",
            (iso_now(), row["outbox_id"], row["claim_token"]),
        )


def enqueue_p0_alert(
    conn: sqlite3.Connection, config: Config, *, case_id: str, revision: int, text: str
) -> list[str]:
    owner_open_id = config.raw["identity"].get("feishu_owner_open_id")
    p0_chat_id = config.raw["identity"].get("feishu_p0_chat_id")
    telegram_chat = config.telegram_control_chat_id
    outbox_ids: list[str] = []
    with transaction(conn):
        if config.notification("telegram_p0") and telegram_chat:
            telegram_id, _ = enqueue_outbox(
                conn,
                channel="telegram",
                action_type="p0_alert",
                destination=f"telegram:{telegram_chat}",
                payload={"text": text},
                idempotency_key=f"{case_id}:p0:{revision}:telegram",
                case_id=case_id,
            )
            outbox_ids.append(telegram_id)
        if config.notification("feishu_p0_message") and owner_open_id and p0_chat_id:
            feishu_id, _ = enqueue_outbox(
                conn,
                channel="feishu_im",
                action_type="send",
                destination=str(p0_chat_id),
                payload={
                    "text": format_feishu_notice_text(text),
                    "format": "markdown",
                    "p0_owner_open_id": str(owner_open_id),
                },
                idempotency_key=f"{case_id}:p0:{revision}:feishu_bot",
                case_id=case_id,
            )
            outbox_ids.append(feishu_id)
    if not outbox_ids:
        raise DeliveryError("P0 has no enabled and configured notification route")
    return outbox_ids
