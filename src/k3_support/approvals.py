from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config
from .db import transaction
from .ids import canonical_json, digest, new_id
from .store import ConflictError, NotFoundError
from .timeutil import iso_now, parse_iso

BOARD_SCOPE = "all_case_scoped_board1_operations"


class ApprovalError(RuntimeError):
    pass


def normalized_board_action(
    case_id: str, session_id: str, estimated_minutes: int
) -> dict[str, Any]:
    if not isinstance(estimated_minutes, int) or not 1 <= estimated_minutes <= 240:
        raise ApprovalError("board estimate must be between 1 and 240 minutes")
    return {
        "board": "board1",
        "case_id": case_id,
        "estimated_minutes": estimated_minutes,
        "scope": BOARD_SCOPE,
        "session_id": session_id,
    }


def normalized_push_action(
    *,
    case_id: str,
    repo: str,
    destination: str,
    commits: list[str],
    command: list[str],
    worktree: str | None = None,
) -> dict[str, Any]:
    if not commits or not command:
        raise ApprovalError("push action needs exact commits and argv")
    action = {
        "case_id": case_id,
        "command": command,
        "commits": commits,
        "destination": destination,
        "mode": "WIP",
        "repo": repo,
    }
    if worktree is not None:
        action["worktree"] = worktree
    return action


def request_approval(
    conn: sqlite3.Connection,
    *,
    approval_type: str,
    case_id: str,
    action: dict[str, Any],
    expires_at: str,
    session_id: str | None = None,
) -> tuple[str, str, bool]:
    parse_iso(expires_at)
    action_digest = digest(action)
    approval_id = new_id("apr")
    now = iso_now()
    with transaction(conn):
        conn.execute(
            """UPDATE approvals SET status='expired',updated_at=?
               WHERE status IN ('requested','approved') AND expires_at<=?""",
            (now, now),
        )
        cursor = conn.execute(
            """INSERT OR IGNORE INTO approvals(approval_id,approval_type,case_id,session_id,status,
                   requested_action_json,action_digest,requested_at,expires_at,created_at,updated_at)
               VALUES(?,?,?,?, 'requested',?,?,?,?,?,?)""",
            (
                approval_id,
                approval_type,
                case_id,
                session_id,
                canonical_json(action),
                action_digest,
                now,
                expires_at,
                now,
                now,
            ),
        )
        if cursor.rowcount == 0:
            row = conn.execute(
                """SELECT approval_id FROM approvals
                     WHERE approval_type=? AND case_id=? AND action_digest=?
                       AND status IN ('requested','approved')
                     ORDER BY requested_at DESC LIMIT 1""",
                (approval_type, case_id, action_digest),
            ).fetchone()
            if row is None:
                raise ApprovalError("active approval conflict could not be resolved")
            return str(row[0]), action_digest, False
    return approval_id, action_digest, True


def verify_control_identity(config: Config, user_id: str, chat_id: str, *, channel: str = "telegram") -> str:
    if channel == "telegram":
        expected = (config.telegram_control_user_id, config.telegram_control_chat_id)
    elif channel == "feishu":
        expected = (config.raw["identity"].get("feishu_control_user_id"), config.raw["identity"].get("feishu_control_chat_id"))
        if not config.raw["identity"].get("control_operator_id"):
            raise ApprovalError("control identity mismatch")
    elif channel == "gui":
        # Authentication and live session/token validation remain the GUI caller's responsibility.
        expected = (config.control_operator_id, config.web_control_chat_id)
    else:
        raise ApprovalError("unsupported control identity channel")
    if not all(isinstance(value, str) and value for value in expected) or (user_id, chat_id) != expected:
        raise ApprovalError("control identity mismatch")
    return config.control_operator_id


def approval_binding(conn, approval_id: str) -> str:
    """Freshness only; caller must authenticate independently."""
    row = conn.execute(
        "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
    ).fetchone()
    if row is None:
        raise ApprovalError("approval not found")
    case = conn.execute(
        "SELECT * FROM cases WHERE case_id=?", (row["case_id"],)
    ).fetchone()
    turn = conn.execute(
        "SELECT * FROM conversation_turns WHERE case_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (row["case_id"],),
    ).fetchone()
    if case is None or case["state"] in {"resolved", "cancelled", "takeover"}:
        raise ApprovalError("Case no longer permits approval; refresh its state")
    if digest(json.loads(row["requested_action_json"])) != row["action_digest"]:
        raise ApprovalError("approval action content mismatch")
    return digest(
        {
            "approval": dict(row),
            "case": dict(case),
            "turn": dict(turn) if turn else None,
        }
    )


def decide_approval(
    conn: sqlite3.Connection,
    config: Config,
    *,
    approval_id: str,
    approve: bool,
    approver_user_id: str,
    approver_chat_id: str,
    message_id: str,
    decision_text: str,
    expected_digest: str,
    control_channel: str = "telegram",
    expected_binding: str | None = None,
) -> dict[str, Any]:
    operator_id = verify_control_identity(config, approver_user_id, approver_chat_id, channel=control_channel)
    if control_channel not in {"telegram", "gui", "feishu"} or (
        control_channel in {"gui", "feishu"} and not expected_binding
    ):
        raise ApprovalError("invalid approval channel or binding")
    expired = False
    result: dict[str, Any] | None = None
    with transaction(conn):
        if (
            expected_binding is not None
            and approval_binding(conn, approval_id) != expected_binding
        ):
            raise ApprovalError("approval details changed; refresh before deciding")
        row = conn.execute(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(approval_id)
        if row["action_digest"] != expected_digest:
            raise ApprovalError("approval digest mismatch")
        if approve and row["approval_type"] == "meeting_create":
            from .mail_meeting_prepare import validate_source

            validate_source(conn, json.loads(row["requested_action_json"]))
        meeting_preview_id = None
        if (
            control_channel in {"gui", "feishu"}
            and approve
            and row["approval_type"] == "meeting_create"
        ):
            from .calendar import exact_meeting_preview

            linked = conn.execute(
                "SELECT preview_id FROM meeting_previews WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            if linked is None:
                raise ApprovalError("meeting requires an exact preview")
            _, meeting_action = exact_meeting_preview(conn, linked[0])
            if meeting_action.get("schema_version") != 2:
                raise ApprovalError("legacy meeting requires a new complete preview")
            meeting_preview_id = linked[0]
        now = datetime.now(UTC)
        if row["status"] in {"approved", "denied"}:
            expected_status = "approved" if approve else "denied"
            if row["status"] == "approved" and parse_iso(row["expires_at"]) <= now:
                conn.execute(
                    "UPDATE approvals SET status='expired',updated_at=? WHERE approval_id=?",
                    (now.isoformat(), approval_id),
                )
                expired = True
            elif (
                row["status"] == expected_status
                and row["approver_channel"] != control_channel
                and row["approver_identity"] == operator_id
            ):
                # Another authenticated entry may acknowledge the same durable
                # decision. Preserve its original audit and never dispatch again.
                result = {**dict(row), "cross_channel_replay": True}
            # A receipt may be lost after the durable decision commits.  Allow
            # the stable owner to repeat the exact command with a new Telegram
            # message ID, but never permit a changed decision or text.
            elif (
                row["status"] == expected_status
                and row["approver_channel"] == control_channel
                and row["approver_identity"] == operator_id
                and row["decision_text"] == decision_text
            ):
                result = dict(row)
            else:
                raise ConflictError(f"approval already {row['status']}")
        elif row["status"] != "requested":
            raise ApprovalError(f"approval is {row['status']}")
        elif parse_iso(row["expires_at"]) <= now:
            conn.execute(
                "UPDATE approvals SET status='expired',updated_at=? WHERE approval_id=?",
                (now.isoformat(), approval_id),
            )
            expired = True
        else:
            status = "approved" if approve else "denied"
            expires_at = row["expires_at"]
            if approve and row["approval_type"] == "board1_lease":
                action = json.loads(row["requested_action_json"])
                expires_at = (
                    now + timedelta(minutes=int(action["estimated_minutes"]))
                ).isoformat()
                existing_lock = conn.execute(
                    "SELECT owner,case_id,expires_at FROM locks WHERE lock_key='board1'",
                ).fetchone()
                lock_owner = f"{row['case_id']}:{row['session_id']}"
                if existing_lock is not None and (
                    existing_lock["owner"] != lock_owner
                    or existing_lock["case_id"] != row["case_id"]
                ):
                    state = (
                        "cleanup pending"
                        if existing_lock["expires_at"] <= now.isoformat()
                        else "already leased"
                    )
                    raise ApprovalError(f"board1 is {state} for another Case/session")
                conn.execute(
                    """INSERT INTO locks(lock_key,owner,case_id,scope,acquired_at,expires_at,
                           heartbeat_at,metadata_json) VALUES('board1',?,?,?,?,?,?,?)
                       ON CONFLICT(lock_key) DO UPDATE SET owner=excluded.owner,case_id=excluded.case_id,
                           scope=excluded.scope,acquired_at=excluded.acquired_at,
                           expires_at=excluded.expires_at,heartbeat_at=excluded.heartbeat_at,
                           metadata_json=excluded.metadata_json""",
                    (
                        lock_owner,
                        row["case_id"],
                        BOARD_SCOPE,
                        now.isoformat(),
                        expires_at,
                        now.isoformat(),
                        canonical_json(
                            {
                                "approval_id": approval_id,
                                "session_id": row["session_id"],
                            }
                        ),
                    ),
                )
            elif approve and row["approval_type"] == "wip_push":
                expires_at = (
                    now
                    + timedelta(
                        minutes=int(config.raw["policy"]["push_approval_minutes"])
                    )
                ).isoformat()
            conn.execute(
                """UPDATE approvals SET status=?,decided_at=?,approver_channel=?,
                       approver_identity=?,approval_message_id=?,decision_text=?,expires_at=?,updated_at=?
                   WHERE approval_id=?""",
                (
                    status,
                    now.isoformat(),
                    control_channel,
                    operator_id,
                    message_id,
                    decision_text,
                    expires_at,
                    now.isoformat(),
                    approval_id,
                ),
            )
            result = dict(
                conn.execute(
                    "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
                ).fetchone()
            )
            if meeting_preview_id is not None:
                conn.execute(
                    "INSERT INTO meeting_dispatch_queue(preview_id,approval_id,state,created_at,updated_at) VALUES(?,?,'queued',?,?)",
                    (meeting_preview_id, approval_id, now.isoformat(), now.isoformat()),
                )
    if expired:
        raise ApprovalError("approval request expired")
    if result is None:
        raise ApprovalError("approval decision did not produce a result")
    return result


def valid_board_lease(
    conn: sqlite3.Connection, *, case_id: str, session_id: str
) -> dict[str, Any] | None:
    now = iso_now()
    rows = conn.execute(
        """SELECT * FROM approvals WHERE approval_type='board1_lease' AND case_id=? AND session_id=?
           AND status='approved' AND consumed_at IS NULL AND expires_at>? ORDER BY decided_at DESC""",
        (case_id, session_id, now),
    ).fetchall()
    for row in rows:
        action = json.loads(row["requested_action_json"])
        lock = conn.execute(
            """SELECT expires_at FROM locks WHERE lock_key='board1' AND owner=?
               AND case_id=? AND scope=? AND expires_at>?""",
            (f"{case_id}:{session_id}", case_id, BOARD_SCOPE, now),
        ).fetchone()
        if (
            lock is not None
            and lock["expires_at"] == row["expires_at"]
            and action
            == normalized_board_action(
                case_id, session_id, int(action["estimated_minutes"])
            )
        ):
            return dict(row)
    return None


def valid_push_approval(
    conn: sqlite3.Connection, *, case_id: str, action: dict[str, Any]
) -> dict[str, Any] | None:
    action_digest = digest(action)
    row = conn.execute(
        """SELECT * FROM approvals WHERE approval_type='wip_push' AND case_id=? AND action_digest=?
           AND status='approved' AND consumed_at IS NULL AND expires_at>?""",
        (case_id, action_digest, iso_now()),
    ).fetchone()
    if row is None or json.loads(row["requested_action_json"]) != action:
        return None
    return dict(row)


def consume_push_approval(
    conn: sqlite3.Connection, approval_id: str, *, expected_digest: str
) -> None:
    now = iso_now()
    with transaction(conn):
        updated = conn.execute(
            """UPDATE approvals SET status='consumed',consumed_at=?,updated_at=?
               WHERE approval_id=? AND approval_type='wip_push' AND status='approved'
               AND consumed_at IS NULL AND action_digest=? AND expires_at>?""",
            (now, now, approval_id, expected_digest, now),
        )
        if updated.rowcount != 1:
            raise ApprovalError("push approval is not valid or was already consumed")


def expiry_after(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()
