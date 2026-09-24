from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_mail_snapshot import item

from k3_support import services
from k3_support.attention_subscriptions import configure


@pytest.mark.parametrize(
    "mode,skipped", [("observe", False), ("paused", True), ("stopped", True)]
)
def test_runtime_mode_gate_uses_shared_capability(
    conn, config, monkeypatch, mode, skipped
):
    config.raw["features"]["mail"] = True
    monkeypatch.setattr(
        "k3_support.runtime_control.current_global_state", lambda *args: {"mode": mode}
    )
    result = services._attention_tick(conn, config)
    assert bool(result.get("skipped")) == skipped


@pytest.mark.parametrize("web_only", [False, True])
def test_maintenance_collects_only_enabled_owner_without_outbox(
    conn, config, monkeypatch, web_only
):
    if web_only:
        config.raw["identity"].update(control_operator_id="shared-owner", telegram_control_user_id=None, telegram_control_chat_id=None)
    config.raw["features"]["mail"] = True
    for owner in (config.control_operator_id, "another-operator"):
        configure(
            conn,
            owner_id=owner,
            category="build_ci",
            enabled=True,
            expected_revision=0,
            request_id=str(uuid4()),
            now=datetime(2026, 8, 1, tzinfo=UTC),
        )
    item(conn, 1)
    before = [tuple(row) for row in conn.execute("SELECT * FROM outbox")]
    monkeypatch.setattr(services, "capability_allowed", lambda *args: True)
    assert services._attention_tick(conn, config)["created"] == 1
    assert services._attention_tick(conn, config)["created"] == 0
    assert [tuple(row) for row in conn.execute("SELECT * FROM outbox")] == before
    item(conn, 2)
    monkeypatch.setattr(services, "capability_allowed", lambda *args: False)
    assert services._attention_tick(conn, config)["skipped"]
    monkeypatch.setattr(services, "capability_allowed", lambda *args: True)
    config.raw["features"]["mail"] = False
    assert services._attention_tick(conn, config)["skipped"]
    assert conn.execute("SELECT count(*) FROM attention_actions").fetchone()[0] == 1
