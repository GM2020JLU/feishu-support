import copy

import pytest

from k3_support import routing
from k3_support.config import Config
from k3_support.lark import LarkError


@pytest.mark.parametrize(
    "expiry", [None, "", "2000-01-01T00:00:00+00:00", "broken", "2999-01-01T00:00:00"]
)
def test_expired_or_ambiguous_auto_identity_not_used(conn, expiry):
    routing.set_requester_profile(
        conn,
        requester_id="p",
        relationship="peer",
        function_role="qa",
        source="feishu_contact",
        expires_at=expiry,
        display_name="cached",
    )
    before = list(conn.iterdump())
    profile = routing.get_requester_profile(conn, "p")
    assert profile["relationship"] == profile["function_role"] == "unknown"
    assert profile["source"] == "unknown" and profile["display_name"] == "cached"
    assert not routing.audience_strategy(profile)["auto_clarify"]
    assert list(conn.iterdump()) == before


def test_failed_refresh_does_not_restore_expired_role(conn, config, monkeypatch):
    routing.set_requester_profile(
        conn,
        requester_id="p",
        relationship="peer",
        function_role="qa",
        source="feishu_contact",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    raw = copy.deepcopy(config.raw)
    raw["identity"]["feishu_owner_open_id"] = "owner"

    def fail(*args):
        raise LarkError("fixture unavailable")

    monkeypatch.setattr(routing, "_fetch_contact_user", fail)
    monkeypatch.setattr(routing, "_search_contact_user", fail)
    assert (
        routing.refresh_requester_profile(
            conn, Config(raw, config.path), requester_id="p"
        )["relationship"]
        == "unknown"
    )


def test_operator_override_keeps_explicit_precedence(conn):
    routing.set_requester_profile(
        conn,
        requester_id="p",
        relationship="supervisor",
        function_role="management",
        source="operator",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    assert routing.get_requester_profile(conn, "p")["relationship"] == "supervisor"
