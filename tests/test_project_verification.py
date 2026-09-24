"""Verification intent never counts as execution evidence."""

import copy
import sqlite3

import pytest
from test_project_bugs import binding

from k3_support import project_bugs as bugs
from k3_support import project_verification as verification


def definition():
    return {
        "title": "Synthetic firmware validation",
        "repositories": [
            {
                "id": "repo",
                "node": "build-node",
                "repository": "firmware",
                "branch": "test",
                "base_commit": "a" * 40,
                "candidate_commit": "b" * 40,
            }
        ],
        "artifacts": [{"id": "image", "sha256": "c" * 64}],
        "devices": [{"id": "board", "node": "device-node", "identity": "fixture-only"}],
        "steps": [
            {
                "id": "function",
                "title": "Reproduce and compare",
                "layer": "device_function",
                "required": True,
                "depends_on": [],
                "repositories": ["repo"],
                "artifacts": ["image"],
                "devices": ["board"],
                "node": "device-node",
                "environment": "Synthetic environment",
                "procedure": "Exercise fixture",
                "oracle": "Expected signal observed",
                "timeout_seconds": 60,
            }
        ],
    }


@pytest.fixture
def request_body(conn):
    bug = binding(conn)
    round_ = bugs.start_round(
        conn,
        bug_id=bug["bug_id"],
        actor="owner",
        request_id="start",
        reason="Synthetic",
        expected_revision=1,
    )
    return {
        "bug_id": bug["bug_id"],
        "round_id": round_["round_id"],
        "actor": "owner",
        "request_id": "plan",
        "expected_revision": 2,
        "plan": definition(),
    }


def test_versions_replay_and_stale_editor(conn, request_body):
    first = verification.publish(conn, **request_body)
    assert verification.publish(conn, **request_body) == first
    assert first["verification_state"] == "not_run"
    assert first["bindings_verified"] is False
    assert first["execution_available"] is False
    changed = copy.deepcopy(request_body)
    changed["plan"]["repositories"][0]["candidate_commit"] = "d" * 40
    with pytest.raises(bugs.BugConflict, match="reused"):
        verification.publish(conn, **changed)
    changed["request_id"] = "next"
    with pytest.raises(bugs.BugConflict, match="changed"):
        verification.publish(conn, **changed)
    changed["expected_revision"] = 3
    second = verification.publish(conn, **changed)
    assert second["version"] == 2 and first["plan_digest"] != second["plan_digest"]
    assert verification.current(conn, request_body["round_id"]) == second
    assert (
        conn.execute("SELECT count(*) FROM project_verification_plans").fetchone()[0]
        == 2
    )
    assert (
        conn.execute("SELECT verification_state FROM project_bug_rounds").fetchone()[0]
        == "not_run"
    )
    assert (
        conn.execute("SELECT count(*) FROM project_bug_operations").fetchone()[0] == 0
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE project_verification_plans SET plan_json='{}'")
    with pytest.raises(sqlite3.IntegrityError, match="retention"):
        conn.execute("DELETE FROM project_verification_plans")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(verified=True),
        lambda p: p.update(steps=[]),
        lambda p: p["steps"][0].update(required=False),
        lambda p: p["steps"][0].update(required=1),
        lambda p: p["steps"][0].update(timeout_seconds=True),
        lambda p: p["steps"][0].update(devices=[]),
        lambda p: p["steps"][0].update(artifacts=[]),
        lambda p: p["steps"][0].update(repositories=["missing"]),
        lambda p: p["steps"][0].update(depends_on=["function"]),
        lambda p: p["steps"][0].update(oracle=""),
        lambda p: p["steps"][0].update(exit_code=0),
        lambda p: p["repositories"][0].update(candidate_commit="HEAD"),
        lambda p: p["artifacts"][0].update(sha256="abc"),
    ],
)
def test_invalid_or_self_attested_plans_rejected(conn, request_body, mutation):
    mutation(request_body["plan"])
    with pytest.raises(ValueError):
        verification.publish(conn, **request_body)
    assert (
        conn.execute("SELECT count(*) FROM project_verification_plans").fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "state", ["running", "unknown"]
)
def test_unsettled_round_not_editable(conn, request_body, state):
    conn.execute("UPDATE project_bug_rounds SET execution_state=?", (state,))
    with pytest.raises(bugs.BugConflict, match="settle"):
        verification.publish(conn, **request_body)


@pytest.mark.parametrize('state',['succeeded','failed','cancelled'])
def test_finished_round_can_publish_verification_without_archiving(conn,request_body,state):
    conn.execute('UPDATE project_bug_rounds SET execution_state=?',(state,))
    plan=verification.publish(conn,**request_body)
    assert plan['round_id']==request_body['round_id'] and plan['verification_state']=='not_run'
    assert conn.execute('SELECT archived_at FROM project_bug_rounds').fetchone()[0] is None


def test_cross_bug_round_rejected(conn, request_body):
    other = binding(conn, item="456")
    with pytest.raises(ValueError, match="matching"):
        verification.publish(conn, **(request_body | {"bug_id": other["bug_id"]}))
