import pytest

from k3_support import budget_settings as settings
from k3_support.model_budget import BudgetError

VALUES = {
    "currency": "USD",
    "daily_limit": "10",
    "case_limit": "2",
    "attempt_limit": "1.000001",
}


def test_preview_apply_replay_and_stale_policy(conn):
    first = settings.preview(conn, session_id="one", values=VALUES, expected_revision=0)
    stale = settings.preview(conn, session_id="two", values=VALUES, expected_revision=0)
    assert first["proposed"]["attempt_limit"] == 1000001
    assert conn.execute("SELECT count(*) FROM model_budget_policy").fetchone()[0] == 0
    with pytest.raises(BudgetError, match="session"):
        settings.apply(
            conn, session_id="two", actor_id="owner", draft_id=first["draft_id"]
        )
    result = settings.apply(
        conn, session_id="one", actor_id="owner", draft_id=first["draft_id"]
    )
    assert result["revision"] == 1 and not result["charges_reset"]
    assert settings.apply(
        conn, session_id="one", actor_id="owner", draft_id=first["draft_id"]
    )["replayed"]
    with pytest.raises(BudgetError, match="changed"):
        settings.apply(
            conn, session_id="two", actor_id="owner", draft_id=stale["draft_id"]
        )
    assert (
        conn.execute("SELECT count(*) FROM model_budget_policy_history").fetchone()[0]
        == 1
    )


def test_expired_draft_rolls_back_and_no_currency_reinterpretation(conn):
    draft = settings.preview(conn, session_id="one", values=VALUES, expected_revision=0)
    conn.execute(
        "UPDATE budget_settings_drafts SET expires_at='2000-01-01T00:00:00+00:00'"
    )
    with pytest.raises(BudgetError, match="expired"):
        settings.apply(
            conn, session_id="one", actor_id="owner", draft_id=draft["draft_id"]
        )
    assert conn.execute("SELECT count(*) FROM model_budget_policy").fetchone()[0] == 0
    assert (
        conn.execute("SELECT applied_revision FROM budget_settings_drafts").fetchone()[
            0
        ]
        is None
    )
    new = settings.preview(conn, session_id="one", values=VALUES, expected_revision=0)
    settings.apply(conn, session_id="one", actor_id="owner", draft_id=new["draft_id"])
    with pytest.raises(BudgetError, match="currency"):
        settings.preview(
            conn,
            session_id="one",
            values={**VALUES, "currency": "CNY"},
            expected_revision=1,
        )


@pytest.mark.parametrize("value", [1, True, "1e3", "0", "-1", "1.0000001", " 1", "NaN"])
def test_amount_is_explicit_and_exact(value):
    with pytest.raises(BudgetError):
        settings.parse_amount(value)
