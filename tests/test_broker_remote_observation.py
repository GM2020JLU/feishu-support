import json
from uuid import uuid4

import pytest
from test_broker_remote_probe import (
    journal_remote as journal_remote,  # noqa: PLC0414 - pytest fixture re-export
)
from test_broker_remote_runner import remote  # noqa: F401

from k3_support.broker_remote_observation import record
from k3_support.case_remote import lines


@pytest.mark.parametrize("kind", ["valid", "unknown", "changed", "crash"])
def test_observation_commits_intent_before_io_and_never_recovers(conn, journal_remote, kind):
    config, _ = journal_remote
    action = conn.execute("SELECT * FROM broker_remote_actions").fetchone()
    observation_id = str(uuid4())
    plan = json.loads(action["plan_json"])
    def transport(**kwargs):
        assert not conn.in_transaction
        assert conn.execute("SELECT state FROM broker_remote_observations").fetchone()[0] == "pending"
        if kind == "crash":
            raise KeyboardInterrupt()
        if kind == "changed":
            conn.execute("UPDATE cases SET version=version+1")
        if kind == "unknown":
            raise OSError("private diagnostic")
        value = {"state": "guardian_returned", "version": 1, "request_id": action["request_id"],
                 "command_digest": plan["command_digest"], "guard_exit_code": 124}
        return {"exit_code": 0, "stdout": json.dumps(value, sort_keys=True) + "\n", "stderr": ""}
    if kind == "crash":
        with pytest.raises(KeyboardInterrupt):
            record(conn, config, observation_id=observation_id, request_id=action["request_id"], transport=transport)
    else:
        result = record(conn, config, observation_id=observation_id, request_id=action["request_id"], transport=transport)
        assert result["state"] == {"valid": "observed", "unknown": "unknown", "changed": "stale"}[kind]
        assert not result["recovery_authorized"]
    replay = record(conn, config, observation_id=observation_id, request_id=action["request_id"],
                    transport=lambda **kw: pytest.fail("observation must not automatically replay"))
    assert replay["historical"] and not replay["recovery_authorized"]
    assert dict(conn.execute("SELECT * FROM broker_remote_actions").fetchone()) == dict(action)
    assert conn.execute("SELECT count(*) FROM broker_remote_results").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    audit = dict(conn.execute("SELECT * FROM broker_remote_observations").fetchone())
    assert "private diagnostic" not in str(audit) and "plan_json" not in str(audit)
    if kind == "crash":
        assert audit["state"] == "pending" and audit["finished_at"] is None
    case_id = conn.execute("SELECT case_id FROM jobs").fetchone()[0]
    assert "最近核对：" in "\n".join(lines(conn, case_id=case_id))
    assert "不是恢复执行许可" in "\n".join(lines(conn, case_id=case_id))
    with pytest.raises(ValueError, match="identity changed"):
        record(conn, config, observation_id=observation_id, request_id=str(uuid4()))


def test_observation_cli_requires_existing_current_database(conn, config, monkeypatch, capsys):
    import yaml

    from k3_support import broker_remote_observation as module
    config.path.write_text(yaml.safe_dump(config.raw))
    calls = []
    def recorded(db, cfg, **kwargs):
        assert not db.in_transaction
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        calls.append(kwargs)
        return {"state": "unknown", "recovery_authorized": False}
    monkeypatch.setattr(module, "record", recorded)
    args = ["--config", str(config.path), "--request-id", str(uuid4()), "--observation-id", str(uuid4())]
    assert module.main(args) == 0 and len(calls) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "unknown"
    conn.execute("DELETE FROM schema_migrations WHERE version=76")
    assert module.main(args) == 1 and len(calls) == 1
    assert conn.execute("SELECT count(*) FROM schema_migrations WHERE version=76").fetchone()[0] == 0
