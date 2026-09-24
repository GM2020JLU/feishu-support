"""Session-bound operator budget drafts; confirmation never clears charges."""

import json
import re
from datetime import UTC, datetime, timedelta

from .db import transaction
from .ids import canonical_json, new_id
from .model_budget import BudgetError, _amount, configure

FIELDS = {"currency", "daily_limit", "case_limit", "attempt_limit"}


def parse_amount(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,6})?", value
    ):
        raise BudgetError(
            "amount must be a positive decimal with at most six fractional digits"
        )
    whole, _, fraction = value.partition(".")
    return _amount(int(whole) * 1000000 + int((fraction + "000000")[:6]))


def preview(conn, *, session_id, values, expected_revision):
    if (
        not isinstance(values, dict)
        or set(values) != FIELDS
        or not isinstance(session_id, str)
        or not session_id
    ):
        raise BudgetError("complete budget fields and session required")
    if not isinstance(values["currency"], str) or not re.fullmatch(
        r"[A-Z]{3}", values["currency"]
    ):
        raise BudgetError("explicit three-letter currency required")
    normalized = {
        key: parse_amount(value) if key != "currency" else value
        for key, value in values.items()
    }
    if (
        not normalized["attempt_limit"]
        <= normalized["case_limit"]
        <= normalized["daily_limit"]
    ):
        raise BudgetError("attempt limit must not exceed Case or daily limit")
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM model_budget_policy WHERE singleton=1"
        ).fetchone()
        revision = row["revision"] if row else 0
        if type(expected_revision) is not int or expected_revision != revision:
            raise BudgetError("policy changed; refresh")
        previous = {key: row[key] for key in FIELDS} if row else None
        if row and row["currency"] != normalized["currency"]:
            raise BudgetError("cannot change currency of existing ledger")
        identifier = new_id("bgd")
        expiry = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
        conn.execute(
            "INSERT INTO budget_settings_drafts VALUES(?,?,?,?,?,?,NULL)",
            (
                identifier,
                session_id,
                revision,
                canonical_json(normalized),
                canonical_json(previous),
                expiry,
            ),
        )
    return {
        "draft_id": identifier,
        "previous": previous,
        "proposed": normalized,
        "expires_at": expiry,
        "amount_unit": "one_millionth_of_currency",
        "charges_reset": False,
        "first_activation": previous is None,
        "warning": "首次应用将启用已接入入口的预算检查；缺模型身份或额度会阻断调用。会话内部费用并非逐次硬限额。",
    }


def apply(conn, *, session_id, actor_id, draft_id):
    row = conn.execute(
        "SELECT * FROM budget_settings_drafts WHERE draft_id=?", (draft_id,)
    ).fetchone()
    if row is None or row["session_id"] != session_id:
        raise BudgetError("budget draft session mismatch")
    if row["applied_revision"] is not None:
        return {
            "revision": row["applied_revision"],
            "replayed": True,
            "charges_reset": False,
        }

    def confirm(db, revision):
        current = db.execute(
            "SELECT * FROM budget_settings_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if dict(current) != dict(row) or datetime.fromisoformat(
            current["expires_at"]
        ) <= datetime.now(UTC):
            raise BudgetError("budget draft changed or expired")
        db.execute(
            "UPDATE budget_settings_drafts SET applied_revision=? WHERE draft_id=?",
            (revision, draft_id),
        )

    revision = configure(
        conn,
        **json.loads(row["values_json"]),
        actor_id=actor_id,
        expected_revision=row["expected_revision"],
        _before_write=confirm,
    )
    return {"revision": revision, "replayed": False, "charges_reset": False}
