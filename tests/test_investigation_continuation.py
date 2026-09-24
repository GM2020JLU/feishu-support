"""Continuation orders scoped jobs without inheriting authority or test verdicts."""

import json
from uuid import uuid4

import pytest
from test_project_investigation import setup
from test_project_verification import definition

from k3_support import project_investigation as investigation
from k3_support.broker_input import project
from k3_support.db import transaction
from k3_support.project_bug_controls import execute
from k3_support.project_investigation_source import selection


def requests(conn, config, tmp_path, state="succeeded"):
    cfg, first = setup(conn, config, tmp_path)
    parent = investigation.submit(conn, cfg, first)
    conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state, parent["job_id"]))
    cfg.raw["repositories"]["kernel"] = {
        **cfg.raw["repositories"]["u-boot"], "path": "/synthetic/kernel",
    }
    second = first | {
        "request_id": "continue-kernel", "repository": "kernel",
        "source": {**first["source"], **selection(cfg, "kernel")},
        "predecessor_job_id": parent["job_id"],
        "expected_revision": conn.execute("SELECT revision FROM project_bugs").fetchone()[0],
    }
    return cfg, parent, second


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
def test_second_repository_and_same_request_replay(conn, config, tmp_path, state):
    cfg, parent, request = requests(conn, config, tmp_path, state)
    child = execute(conn, cfg, action="continue-investigation-job", payload=request)
    assert child["created"]
    assert execute(conn, cfg, action="continue-investigation-job", payload=request) == child | {"created": False}
    payload = json.loads(conn.execute("SELECT payload_json FROM broker_inputs WHERE job_id=?", (child["job_id"],)).fetchone()[0])
    assert payload["repos"] == ["kernel"]
    assert payload["context_extra"]["project_investigation"]["predecessor_job_id"] == parent["job_id"]
    assert conn.execute("SELECT 1 FROM project_bug_events WHERE kind='coding_job_created' "
                        "AND json_extract(detail_json,'$.job_id')=? "
                        "AND json_extract(detail_json,'$.predecessor_job_id')=?",
                        (child["job_id"], parent["job_id"])).fetchone()
    assert "not proof of a successful fix" in payload["brief"]
    round_ = conn.execute("SELECT * FROM project_bug_rounds").fetchone()
    assert (round_["execution_state"], round_["repair_state"], round_["verification_state"]) == ("running", "not_started", "not_run")
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    with pytest.raises(ValueError, match="different content"):
        execute(conn, cfg, action="continue-investigation-job", payload=request | {"predecessor_job_id": "other"})


@pytest.mark.parametrize("state", ["queued", "running", "waiting", "orphaned"])
def test_unsettled_predecessor_cannot_continue(conn, config, tmp_path, state):
    cfg, _, request = requests(conn, config, tmp_path, state)
    with pytest.raises(ValueError, match="settled job"):
        investigation.submit(conn, cfg, request)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_existing_verification_plan_requires_new_round(conn, config, tmp_path):
    from k3_support.project_verification import publish

    cfg, _, request = requests(conn, config, tmp_path)
    publish(conn, bug_id=request["bug_id"], round_id=request["round_id"], actor=cfg.control_operator_id,
            request_id="plan", expected_revision=request["expected_revision"], plan=definition())
    request["expected_revision"] += 1
    with pytest.raises(ValueError, match="preserve the existing verification plan"):
        investigation.submit(conn, cfg, request)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_predecessor_reactivation_fences_worker_input(conn, config, tmp_path):
    cfg, parent, request = requests(conn, config, tmp_path)
    child = investigation.submit(conn, cfg, request)
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (child["job_id"],)).fetchone()
    def read():
        with transaction(conn):
            return project(conn, job_id=row["job_id"], case_id=row["case_id"],
                           lifecycle_round=row["lifecycle_round"], input_digest=row["input_digest"], request_id=str(uuid4()))
    assert read()["repos"] == ["kernel"]
    conn.execute("UPDATE jobs SET state='running' WHERE job_id=?", (parent["job_id"],))
    with pytest.raises(ValueError, match="settled job"):
        read()


def test_late_revision_change_rolls_back_continuation(conn, config, tmp_path, monkeypatch):
    from pathlib import Path

    cfg, _, request = requests(conn, config, tmp_path)
    original = Path.write_text
    def changed(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.name == ".capability":
            conn.execute("UPDATE project_bugs SET revision=revision+1")
        return result
    monkeypatch.setattr(Path, "write_text", changed)
    with pytest.raises(ValueError, match="Bug changed"):
        investigation.submit(conn, cfg, request)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert len(list(cfg.data_dir.rglob(".capability"))) == 1


def test_terminal_job_with_unsettled_resources_cannot_continue(conn, config, tmp_path):
    from test_investigation_candidate import seed

    cfg, first, parent, _, _ = seed(conn, config, tmp_path)
    conn.execute("DELETE FROM broker_execution_resources")
    request = first | {"predecessor_job_id": parent, "request_id": "continue",
                       "expected_revision": conn.execute("SELECT revision FROM project_bugs").fetchone()[0]}
    with pytest.raises(ValueError, match="resources_unsettled"):
        investigation.submit(conn, cfg, request)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_foreign_predecessor_is_not_accepted(conn, config, tmp_path):
    cfg, _, request = requests(conn, config, tmp_path)
    with pytest.raises(ValueError, match="settled job in this investigation"):
        investigation.submit(conn, cfg, request | {"predecessor_job_id": "foreign-job"})
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
