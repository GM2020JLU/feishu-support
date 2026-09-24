import json
from uuid import uuid4

import pytest
from test_broker_remote_probe import (
    journal_remote as journal_remote,  # noqa: PLC0414 - pytest fixture re-export
)
from test_broker_remote_runner import remote  # noqa: F401

from k3_support.broker_execution_instances import register
from k3_support.broker_remote_cleanup import apply, preview
from k3_support.broker_remote_observation import record
from k3_support.broker_remote_runner import run_one
from k3_support.broker_remote_state import UNSETTLED


@pytest.fixture
def cleanup_candidate(conn, journal_remote):
    cfg, reader = journal_remote
    # Use the real bound instance registration fixture's original binding.
    instance = "a" * 32
    grant = conn.execute("SELECT * FROM broker_grants").fetchone()
    claim = conn.execute("SELECT request_id FROM broker_claim_receipts").fetchone()[0]
    register(conn, grant_id=grant["grant_id"], claim_request_id=claim, invocation_id=instance,
             cgroup_path=f"/system.slice/k3-support-broker-worker@{claim}.service")
    conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)", (grant["grant_id"], instance, 1234, 1, 0, "fixture"))
    conn.execute("UPDATE broker_remote_actions SET state='unknown'")
    action = conn.execute("SELECT * FROM broker_remote_actions").fetchone()
    plan = json.loads(action["plan_json"])
    observation_id = str(uuid4())
    def transport(**kwargs):
        value = {"state": "guardian_returned", "version": 1, "request_id": action["request_id"],
                 "command_digest": plan["command_digest"], "guard_exit_code": 124}
        return {"exit_code": 0, "stdout": json.dumps(value, sort_keys=True)+"\n", "stderr": ""}
    record(conn, cfg, request_id=action["request_id"], observation_id=observation_id, transport=transport)
    return cfg, reader, {"request_id": action["request_id"], "observation_id": observation_id}


def unresolved(conn):
    return bool(conn.execute("SELECT 1 FROM broker_remote_actions a LEFT JOIN broker_remote_results r USING(request_id) "
                             f"WHERE {UNSETTLED}").fetchone())


def test_cleanup_releases_only_remote_occupancy_preserving_unknown_result(conn, cleanup_candidate):
    cfg, reader, ids = cleanup_candidate
    from k3_support.case_content_inventory import preview as retention_preview
    case_id = conn.execute('SELECT case_id FROM jobs LIMIT 1').fetchone()[0]
    def retained():
        return any(h['reason']=='remote_execution_unsettled' for h in retention_preview(conn, case_id)['observed_holds'])
    assert unresolved(conn)
    assert retained()
    before = [dict(r) for r in conn.execute("SELECT * FROM broker_remote_actions")]
    checked = preview(conn, cfg, **ids)
    assert apply(conn, cfg, **ids, preview_digest=checked["preview_digest"])["state"] == "cleanup_recorded"
    assert not unresolved(conn)
    assert not retained()
    assert apply(conn, cfg, **ids, preview_digest=checked["preview_digest"])["state"] == "already_recorded"
    assert run_one(conn, cfg, contract_reader=reader, transport=lambda **kw: pytest.fail("must not reexecute"))["state"] == "idle"
    assert before == [dict(r) for r in conn.execute("SELECT * FROM broker_remote_actions")]
    assert conn.execute("SELECT count(*) FROM broker_remote_results").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    conn.execute("UPDATE broker_remote_actions SET updated_at='changed'")
    assert unresolved(conn)
    assert retained()


@pytest.mark.parametrize("change", ["case", "exit", "observation", "digest", "expired", "contradiction"])
def test_cleanup_rejects_stale_or_unverified_preview(conn, cleanup_candidate, change):
    cfg, _, ids = cleanup_candidate
    checked = preview(conn, cfg, **ids)
    if change == "case":
        conn.execute("UPDATE cases SET version=version+1")
    elif change == "exit":
        conn.execute("DELETE FROM broker_service_exits")
    elif change == "observation":
        conn.execute("UPDATE broker_remote_observations SET state='stale'")
    elif change == "expired":
        conn.execute("UPDATE broker_remote_observations SET finished_at='2000-01-01T00:00:00+00:00'")
    elif change == "contradiction":
        conn.execute("INSERT INTO broker_remote_results VALUES(?,?,?,?,?)", (ids["request_id"], 0, "", "", "fixture"))
    else:
        checked["preview_digest"] = "0" * 64
    with pytest.raises(ValueError):
        apply(conn, cfg, **ids, preview_digest=checked["preview_digest"])
    assert unresolved(conn)
    assert conn.execute("SELECT count(*) FROM broker_remote_cleanup").fetchone()[0] == 0


def test_corrected_observation_invalidates_previous_cleanup(conn, cleanup_candidate):
    cfg, _, ids = cleanup_candidate
    checked = preview(conn, cfg, **ids)
    apply(conn, cfg, **ids, preview_digest=checked["preview_digest"])
    assert not unresolved(conn)
    conn.execute("UPDATE broker_remote_observations SET result_json='{}'")
    assert unresolved(conn)


def test_cli_cleanup_preview_and_explicit_apply(conn, cleanup_candidate, capsys):
    import yaml

    from k3_support.broker_remote_observation import main
    cfg, _, ids = cleanup_candidate
    cfg.path.write_text(yaml.safe_dump(cfg.raw))
    args = ["--config", str(cfg.path), "--request-id", ids["request_id"], "--observation-id", ids["observation_id"]]
    assert main([*args, "--cleanup-preview"]) == 0
    candidate = json.loads(capsys.readouterr().out)
    assert unresolved(conn)
    assert main([*args, "--cleanup-apply", candidate["preview_digest"]]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "cleanup_recorded"
    assert not unresolved(conn)
