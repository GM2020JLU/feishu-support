import copy

import pytest

from k3_support import routing
from k3_support.config import Config
from k3_support.db import connect


@pytest.mark.parametrize("initial", [False, True])
def test_late_contact_response_does_not_overwrite_operator(
    conn, config, monkeypatch, initial
):
    if initial:
        routing.set_requester_profile(
            conn,
            requester_id="person",
            relationship="unknown",
            function_role="unknown",
            source="unknown",
        )
    raw = copy.deepcopy(config.raw)
    raw["identity"]["feishu_owner_open_id"] = "owner"
    calls = []

    def fetch(identifier, runner):
        calls.append(identifier)
        if identifier == "person":
            other = connect(config.database_path)
            try:
                routing.set_requester_profile(
                    other, requester_id="person", relationship="supervisor",
                    function_role="management", source="operator",
                    evidence={"operator": "owner"},
                )
            finally:
                other.close()
        return {"name": "late directory value", "job_title": "engineer"}

    monkeypatch.setattr(routing, "_fetch_contact_user", fetch)
    result = routing.refresh_requester_profile(
        conn, Config(raw, config.path), requester_id="person"
    )
    assert calls == ["person", "owner"]
    assert result["source"] == "operator" and result["relationship"] == "supervisor"
    assert (
        conn.execute("SELECT display_name FROM requester_profiles").fetchone()[0]
        is None
    )
