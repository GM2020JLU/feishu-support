from datetime import UTC, datetime

import pytest
from test_broker_connection import exchange
from test_broker_receipts import setup
from test_review import active_config

from k3_support.broker_renew import renew
from k3_support.runtime_control import ensure_global_state

NOW = datetime(2026, 9, 8, 0, 30, tzinfo=UTC)


@pytest.mark.parametrize("mode", ["paused", "stopped", "observe"])
def test_mode_change_rejects_even_previous_successful_renewal(conn, config, mode):
    request = setup(conn)
    cfg = active_config(config)
    renew(conn, request, peer_uid=1234, now=NOW, config=cfg)
    ensure_global_state(conn)
    conn.execute("UPDATE global_control_state SET mode=?", (mode,))
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="disabled"):
        renew(conn, request, peer_uid=1234, now=NOW, config=cfg)
    assert list(conn.iterdump()) == before


def test_socket_uses_control_configuration_for_renewal(conn, config, monkeypatch):
    request = setup(conn)
    result = exchange(conn, monkeypatch, request, config=config)
    assert result["ok"] is False
    assert conn.execute("SELECT count(*) FROM broker_receipts").fetchone()[0] == 0
