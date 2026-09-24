from datetime import UTC, datetime
from uuid import uuid4

import pytest

from k3_support import services
from k3_support.store import create_case
from k3_support.watch_subscriptions import configure


@pytest.mark.parametrize("mode,skipped", [("observe", False), ("paused", True), ("stopped", True)])
def test_watch_tick_obeys_runtime_mode(conn, config, monkeypatch, mode, skipped):
    monkeypatch.setattr("k3_support.runtime_control.current_global_state", lambda *args: {"mode": mode})
    assert bool(services._watch_tick(conn, config).get("skipped")) == skipped


def test_watch_tick_materializes_only_owner_and_rolls_back_partial_failure(conn, config, monkeypatch):
    case_id, _ = create_case(conn, title="synthetic", case_type="bug", severity="P3", confidence=0.8)
    for owner in (config.telegram_control_user_id, "other"):
        configure(conn, owner_id=owner, source_kind="case", source_key=case_id, enabled=True,
                  expected_revision=0, request_id=str(uuid4()), now=datetime(2026, 8, 1, tzinfo=UTC))
    config.raw["features"]["mail"] = False
    monkeypatch.setattr(services, "capability_allowed", lambda *args: True)
    assert services._watch_tick(conn, config)["cases"] == 1
    assert services._watch_tick(conn, config)["created"] == 0
    assert conn.execute("SELECT count(*) FROM watch_actions").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0

    def first(conn, **kwargs):
        conn.execute("UPDATE watch_subscriptions SET enabled=0")
        return {"created": 0}

    def failed(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    before = list(conn.iterdump())
    monkeypatch.setattr("k3_support.watch_subscriptions.collect_releases", first)
    monkeypatch.setattr("k3_support.watch_subscriptions.collect_cases", failed)
    with pytest.raises(RuntimeError, match="synthetic"):
        services._watch_tick(conn, config)
    assert list(conn.iterdump()) == before
