"""Accounting reservations, not execution authority or provider cost proof.

Amounts are integer millionths of the policy currency. Unknown outcomes keep
their entire reservation. Only trusted transport receipts may settle charges.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .db import transaction
from .ids import canonical_json, digest, new_id
from .timeutil import iso_now

MAX_AMOUNT = 10**12


class BudgetError(ValueError):
    pass


def _amount(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= MAX_AMOUNT:
        raise BudgetError("invalid integer budget amount")
    return value


def configure(
    conn,
    *,
    currency,
    daily_limit,
    case_limit,
    attempt_limit,
    actor_id,
    expected_revision,
    _before_write=None,
):
    """Trusted operator entry only. Changing limits does not reset consumption."""
    if (
        not isinstance(currency, str)
        or not re.fullmatch(r"[A-Z]{3}", currency)
        or not actor_id
    ):
        raise BudgetError("explicit currency and operator are required")
    for value in (daily_limit, case_limit, attempt_limit):
        _amount(value)
    if not attempt_limit <= case_limit <= daily_limit:
        raise BudgetError("attempt limit must not exceed Case or daily limit")
    with transaction(conn):
        old = conn.execute(
            "SELECT * FROM model_budget_policy WHERE singleton=1"
        ).fetchone()
        revision = old["revision"] if old else 0
        if type(expected_revision) is not int or expected_revision != revision:
            raise BudgetError("budget policy changed; refresh before editing")
        if old and currency != old["currency"]:
            raise BudgetError("cannot reinterpret existing charges in another currency")
        if _before_write is not None:
            _before_write(conn, revision + 1)
        conn.execute(
            """INSERT INTO model_budget_policy VALUES(1,?,?,?,?,?,?,?)
            ON CONFLICT(singleton) DO UPDATE SET daily_limit=excluded.daily_limit,
            case_limit=excluded.case_limit,attempt_limit=excluded.attempt_limit,
            revision=excluded.revision,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (
                currency,
                daily_limit,
                case_limit,
                attempt_limit,
                revision + 1,
                actor_id,
                iso_now(),
            ),
        )
        conn.execute(
            "INSERT INTO model_budget_policy_history VALUES(?,?,?,?)",
            (
                revision + 1,
                canonical_json(
                    {
                        "currency": currency,
                        "daily_limit": daily_limit,
                        "case_limit": case_limit,
                        "attempt_limit": attempt_limit,
                    }
                ),
                actor_id,
                iso_now(),
            ),
        )
    return revision + 1


def reserve(
    conn, *, request_id, case_id, provider, model, amount, input_digest, at=None
):
    """Return created=False on replay; that is not permission to call again."""
    with transaction(conn):
        return _reserve(conn, request_id=request_id, case_id=case_id,
                        provider=provider, model=model, amount=amount,
                        input_digest=input_digest, at=at)


def _reserve(conn, *, request_id, case_id, provider, model, amount, input_digest, at=None):
    if not conn.in_transaction:
        raise BudgetError("reservation requires a control transaction")
    _amount(amount)
    if any(
        not isinstance(value, str) or not value or len(value) > 200
        for value in (request_id, provider, model)
    ):
        raise BudgetError("bounded request/provider/model identities are required")
    if not isinstance(input_digest, str) or not re.fullmatch(
        r"[a-f0-9]{64}", input_digest
    ):
        raise BudgetError("input digest must identify the exact call")
    observed = at or datetime.now(UTC)
    if observed.tzinfo is None:
        raise BudgetError("budget time must include a timezone")
    day = observed.astimezone(UTC).date().isoformat()
    fingerprint = digest(
        {
            "case_id": case_id,
            "provider": provider,
            "model": model,
            "amount": amount,
            "input_digest": input_digest,
        }
    )
    previous = conn.execute(
        "SELECT * FROM model_budget_attempts WHERE request_id=?", (request_id,)
    ).fetchone()
    if previous:
        if previous["request_digest"] != fingerprint:
            raise BudgetError("model request ID reused with different content")
        return {**dict(previous), "created": False}
    policy = conn.execute(
        "SELECT * FROM model_budget_policy WHERE singleton=1"
    ).fetchone()
    if policy is None:
        raise BudgetError(
            "budget is unconfigured; operator must set explicit limits"
        )
    if (
        case_id is not None
        and conn.execute(
            "SELECT 1 FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        is None
    ):
        raise BudgetError("unknown Case budget scope")
    daily = conn.execute(
        "SELECT coalesce(sum(charged),0) FROM model_budget_attempts WHERE budget_day=?",
        (day,),
    ).fetchone()[0]
    case_total = conn.execute(
        "SELECT coalesce(sum(charged),0) FROM model_budget_attempts WHERE case_id IS ? AND (? IS NOT NULL OR budget_day=?)",
        (case_id, case_id, day),
    ).fetchone()[0]
    # Pre-Case calls still share a bounded bucket, never unlimited allowance.
    if (
        amount > policy["attempt_limit"]
        or daily + amount > policy["daily_limit"]
        or case_total + amount > policy["case_limit"]
    ):
        raise BudgetError(
            "budget exhausted; await operator adjustment or a verified receipt"
        )
    attempt_id, now = new_id("mba"), iso_now()
    conn.execute(
        """INSERT INTO model_budget_attempts(attempt_id,request_id,request_digest,case_id,budget_day,
            provider,model,currency,policy_revision,reserved,charged,state,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'reserved',?,?)""",
        (
            attempt_id,
            request_id,
            fingerprint,
            case_id,
            day,
            provider,
            model,
            policy["currency"],
            policy["revision"],
            amount,
            amount,
            now,
            now,
        ),
    )
    result = dict(
        conn.execute(
            "SELECT * FROM model_budget_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
    )
    return {**result, "created": True}


def dispatch(conn, attempt_id):
    """CAS before the transport: only its single winner may start that call."""
    with transaction(conn):
        return _dispatch(conn, attempt_id)


def _dispatch(conn, attempt_id):
    if not conn.in_transaction:
        raise BudgetError("dispatch requires a control transaction")
    attempt = conn.execute(
        "SELECT * FROM model_budget_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if attempt is None or attempt["state"] != "reserved":
        return False
    policy = conn.execute("SELECT revision FROM model_budget_policy WHERE singleton=1").fetchone()
    if policy is None or policy["revision"] != attempt["policy_revision"]:
        conn.execute(
            "UPDATE model_budget_attempts SET state='cancelled',charged=0,updated_at=? WHERE attempt_id=? AND state='reserved'",
            (iso_now(),attempt_id),
        )
        return False
    return (
        conn.execute(
            "UPDATE model_budget_attempts SET state='dispatched',updated_at=? WHERE attempt_id=? AND state='reserved'",
            (iso_now(), attempt_id),
        ).rowcount
        == 1
    )


def authorize_start(conn, *, record_start, before_dispatch=None, **reservation):
    """Atomically charge and record one start in the trusted control process.

    record_start(conn, attempt_id) must only validate/write local database state.
    It must not start processes or perform network I/O: commit precedes launch.
    Provider identity must already be bound by the caller; worker claims are not
    a valid identity source. This does not enable the unfinished broker path.
    before_dispatch, when supplied, must be a bounded local identity read, not
    network I/O or another transaction. Failed checks commit a cancelled
    tombstone without spending budget; they never authorize a replay.
    """
    interrupted = None
    valid = True
    with transaction(conn):
        attempt = _reserve(conn, **reservation)
        if not attempt["created"]:
            raise BudgetError("model call already reserved; reconcile before retry")
        if before_dispatch is not None:
            try:
                valid = before_dispatch() is True
            except (KeyboardInterrupt, SystemExit) as error:
                valid, interrupted = False, error
            except Exception:  # noqa: BLE001 - do not expose private configuration
                valid = False
        if valid:
            if not _dispatch(conn, attempt["attempt_id"]):
                raise BudgetError("budget dispatch denied")
            record_start(conn, attempt["attempt_id"])
        else:
            conn.execute(
                "UPDATE model_budget_attempts SET state='cancelled',charged=0,updated_at=? WHERE attempt_id=?",
                (iso_now(), attempt["attempt_id"]),
            )
    if interrupted is not None:
        raise interrupted
    if not valid:
        raise BudgetError("pre-dispatch identity changed; model not started")
    return {**attempt, "state": "dispatched"}


def cancel_before_dispatch(conn, attempt_id):
    with transaction(conn):
        return (
            conn.execute(
                "UPDATE model_budget_attempts SET state='cancelled',charged=0,updated_at=? WHERE attempt_id=? AND state='reserved'",
                (iso_now(), attempt_id),
            ).rowcount
            == 1
        )


def mark_unknown(conn, attempt_id):
    with transaction(conn):
        return (
            conn.execute(
                "UPDATE model_budget_attempts SET state='unknown',updated_at=? WHERE attempt_id=? AND state='dispatched'",
                (iso_now(), attempt_id),
            ).rowcount
            == 1
        )


def settle(conn, attempt_id, *, receipt):
    """Caller authenticates provider receipt; never feed model-generated usage."""
    if not isinstance(receipt, dict) or set(receipt) != {
        "receipt_id",
        "request_id",
        "provider",
        "model",
        "currency",
        "cost",
    }:
        raise BudgetError("exact provider cost receipt is required")
    _amount(receipt["cost"], zero=True)
    if (
        not isinstance(receipt["receipt_id"], str)
        or not 1 <= len(receipt["receipt_id"]) <= 200
    ):
        raise BudgetError("invalid receipt ID")
    encoded = canonical_json(receipt)
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM model_budget_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise BudgetError("budget attempt not found")
        if row["state"] == "settled":
            if row["receipt_json"] != encoded:
                raise BudgetError("settled cost cannot be silently rewritten")
            return dict(row)
        if row["state"] not in {"dispatched", "unknown"}:
            raise BudgetError("receipt requires a dispatched call")
        if receipt["request_id"] != row["request_id"]:
            raise BudgetError("receipt belongs to a different model request")
        if any(receipt[key] != row[key] for key in ("provider", "model", "currency")):
            raise BudgetError("receipt identity differs from reserved call")
        if conn.execute(
            "SELECT 1 FROM model_budget_attempts WHERE receipt_id=?",
            (receipt["receipt_id"],),
        ).fetchone():
            raise BudgetError("provider receipt already used")
        # If real cost exceeds the reserve, record the debt honestly; future
        # reservations see it. This ledger alone is not a hard billing cap.
        conn.execute(
            "UPDATE model_budget_attempts SET state='settled',charged=?,receipt_id=?,receipt_json=?,updated_at=? WHERE attempt_id=?",
            (receipt["cost"], receipt["receipt_id"], encoded, iso_now(), attempt_id),
        )
        return dict(
            conn.execute(
                "SELECT * FROM model_budget_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        )


def invoke(conn, *, transport, trusted_receipt_reader=None, before_dispatch=None, **reservation):
    """Caller supplies authenticated scope and an already bounded transport.

    The receipt reader must read provider metadata, not the model's generated
    JSON. Missing or invalid receipts retain the reservation even on success.
    """
    attempt = authorize_start(conn, record_start=lambda *_: None,
                              before_dispatch=before_dispatch, **reservation)
    identifier = attempt["attempt_id"]
    try:
        value = transport()
    except (KeyboardInterrupt, SystemExit):
        # After dispatch we cannot infer whether the provider incurred cost.
        mark_unknown(conn, identifier)
        raise
    except Exception:  # noqa: BLE001 - never expose input-bearing provider exceptions
        mark_unknown(conn, identifier)
        raise BudgetError(
            "model result/cost uncertain; reservation retained, no automatic retry"
        ) from None
    # Persist uncertainty before parsing any optional receipt. A process crash
    # or receipt parser error must not release money already possibly spent.
    mark_unknown(conn, identifier)
    cost = None
    if trusted_receipt_reader is not None:
        try:
            receipt = trusted_receipt_reader(value)
            if receipt is not None:
                cost = settle(conn, identifier, receipt=receipt)
        except Exception:  # noqa: BLE001 - malformed provider usage stays charged at reserve
            cost = None
    return {
        "value": value,
        "attempt_id": identifier,
        "cost_state": "settled" if cost is not None else "unknown",
        "charged": cost["charged"] if cost is not None else attempt["reserved"],
    }


def snapshot(conn):
    from .budget_blocks import report

    policy = conn.execute(
        "SELECT * FROM model_budget_policy WHERE singleton=1"
    ).fetchone()
    return {
        "configured": policy is not None,
        "blocks": report(conn),
        "coverage": {
            "guarded_when_configured": ["message_semantics", "research_semantics", "mail_summary", "mail_classification", "release_impact", "supervised_coding_session"],
            "readonly_evaluation": "blocked_if_reservation_cannot_be_written",
            "not_guarded": ["coding_internal_model_turns", "external_direct_model_calls"],
            "global_budget_enforced": False,
            "provider_billing_verified": False,
        },
        "policy": dict(policy) if policy else None,
        "charges": [
            dict(row)
            for row in conn.execute(
                "SELECT budget_day,currency,state,count(*) attempts,sum(charged) charged FROM model_budget_attempts GROUP BY budget_day,currency,state ORDER BY budget_day DESC"
            )
        ],
        "unknown_cost_is_zero": False,
    }
