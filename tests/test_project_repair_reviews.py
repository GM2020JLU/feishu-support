"""Repair decisions use complete, current source evidence and stay independent."""

import base64
import hashlib
import json
import sqlite3
from uuid import uuid4

import pytest

from test_investigation_candidate import seed

from k3_support import project_bug_controls as controls
from k3_support import project_bugs as bugs
from k3_support import project_investigation as investigation
from k3_support import project_repair_reviews as repair
from k3_support.ids import canonical_json
from k3_support.project_investigation_source import selection


def changeset(*, base="a" * 40, head="b" * 40, untracked=(), tracked=True):
    patch = b"diff --git a/source.c b/source.c\nindex 1..2 100644\n--- a/source.c\n+++ b/source.c\n@@ -1 +1 @@\n-old\n+new\n"
    return {
        "state": "observed",
        "coverage": "complete_committed_changeset_v1",
        "base_commit": base,
        "head_commit": head,
        "commits": [head] if base != head else [],
        "paths_b64": base64.b64encode(b"source.c\0").decode(),
        "patch_b64": base64.b64encode(patch).decode(),
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "untracked_paths_b64": base64.b64encode(b"".join(path.encode() + b"\0" for path in untracked)).decode(),
        "tracked_content_matches": tracked,
    }


def setup_evidence(conn, config, tmp_path, *, evidence=True, untracked=()):
    evidence_value = changeset(untracked=untracked) if evidence else None
    cfg, task, job, case_id, _source = seed(
        conn, config, tmp_path, changeset=evidence_value
    )
    cfg.raw["identity"]["control_operator_id"] = "repair-owner"
    return cfg, task, job, case_id


def detail(conn, cfg, task):
    return controls.execute(
        conn,
        cfg,
        action="repair-review-detail",
        payload={"bug_id": task["bug_id"], "round_id": task["round_id"]},
    )


def review_payload(view, task, *, verdict="ready", request_id=None, expected=None, digest=None):
    return {
        "bug_id": task["bug_id"],
        "round_id": task["round_id"],
        "request_id": request_id or str(uuid4()),
        "evidence_digest": digest or view["evidence_digest"],
        "expected_review_id": (
            expected if expected is not None else view["review"]["review_id"] if view["review"] else None
        ),
        "verdict": verdict,
        "rationale": "Operator reviewed the complete changeset and repository scope.",
        "attested": True,
    }


def record(conn, cfg, payload):
    return controls.execute(conn, cfg, action="record-repair-review", payload=payload)


def settle_repository_job(conn, cfg, task, job_id, *, head):
    """Insert explicitly synthetic broker receipts for the control evidence path."""
    job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    input_row = conn.execute("SELECT payload_json FROM broker_inputs WHERE job_id=?", (job_id,)).fetchone()
    inputs = json.loads(input_row["payload_json"])
    context = inputs["context_extra"]["project_investigation"]
    source = context["source"]
    repository = inputs["repos"][0]
    case_id = job["case_id"]
    grant_id = f"repair-grant-{job_id}"
    request_id = str(uuid4())
    root = str(cfg.runtime("remote_worktree_root")) + "/" + case_id + "/investigation-" + job_id
    binding = {
        "job_id": job_id,
        "request_id": request_id,
        "root": root,
        "repository": repository,
        "base_commit": source["base_commit"],
    }
    conn.execute(
        "INSERT INTO broker_grants VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
        (grant_id, "e" * 64, 1234, job_id, job["attempt_no"], job["lifecycle_round"],
         job["input_digest"], "fixture", "now", "later"),
    )
    conn.execute(
        "INSERT INTO broker_execution_starts VALUES(?,?,?,?,?,?)",
        (grant_id, job_id, job["attempt_no"], "start-" + job_id, 1234, "now"),
    )
    conn.execute(
        "INSERT INTO broker_execution_resources VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (grant_id, "claim-" + job_id, job_id, job["attempt_no"], job["lifecycle_round"],
         job["input_digest"], case_id, None, 1, "now", "settled"),
    )
    conn.execute(
        "INSERT INTO broker_remote_actions VALUES(?,1234,?,?,?,'succeeded','now','now')",
        (request_id, grant_id, "fixture", canonical_json({"investigation_checkout": binding})),
    )
    conn.execute("INSERT INTO broker_remote_results VALUES(?,0,?,?,?)", (request_id, "", "", "now"))

    source_observation = {
        "repository": repository,
        "path": root + "/repository",
        "head": head,
        "end_head": head,
        "base_is_ancestor": True,
        "tracked_content_matches": True,
        "matched": False,
        "tracked_count": 1,
        "coverage": "tracked_source_sample",
    }
    patch = (f"diff --git a/{repository}.c b/{repository}.c\n"
             f"index 1..2 100644\n--- a/{repository}.c\n+++ b/{repository}.c\n"
             "@@ -1 +1 @@\n-old\n+new\n").encode()
    changeset_value = {
        "state": "observed",
        "coverage": "complete_committed_changeset_v1",
        "base_commit": source["base_commit"],
        "head_commit": head,
        "commits": [head],
        "paths_b64": base64.b64encode((repository + ".c\0").encode()).decode(),
        "patch_b64": base64.b64encode(patch).decode(),
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "untracked_paths_b64": "",
        "tracked_content_matches": True,
    }
    observation = {
        "state": "clean",
        "source": source_observation,
        "changeset": changeset_value,
    }
    conn.execute(
        "INSERT INTO project_investigation_checkout_after VALUES(?,?,?,?,?)",
        (request_id, job_id, "clean", canonical_json(observation), "now"),
    )
    return request_id


def test_detail_record_replay_derives_ready_without_verification_or_remote_write(conn, config, tmp_path):
    cfg, task, _job, _case = setup_evidence(conn, config, tmp_path)
    view = detail(conn, cfg, task)
    assert view["can_mark_ready"] is True
    assert view["ready_blockers"] == []
    assert view["repositories"][0]["commits"] == ["b" * 40]

    request = review_payload(view, task)
    receipt = record(conn, cfg, request)
    before_replay = list(conn.iterdump())
    assert record(conn, cfg, request) == receipt
    assert list(conn.iterdump()) == before_replay

    current = detail(conn, cfg, task)
    assert current["repair_state"] == "ready"
    assert current["review_state"] == "current"
    assert current["review"]["review_id"] == receipt["review_id"]
    assert current["verification_passed"] is False
    assert current["closure_authorized"] is False
    assert conn.execute("SELECT verification_state FROM project_bug_rounds WHERE round_id=?", (task["round_id"],)).fetchone()[0] == "not_run"
    assert conn.execute("SELECT count(*) FROM project_verification_runs").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM project_bug_operations").fetchone()[0] == 0
    event = conn.execute("SELECT kind FROM project_bug_events WHERE round_id=? ORDER BY rowid DESC LIMIT 1", (task["round_id"],)).fetchone()
    assert event["kind"] == "repair_reviewed"


def test_repair_control_requires_exact_fields_and_configured_actor(conn, config, tmp_path):
    cfg, task, _job, _case = setup_evidence(conn, config, tmp_path)
    view = detail(conn, cfg, task)
    request = review_payload(view, task)
    with pytest.raises(ValueError, match="exact request fields"):
        controls.execute(conn, cfg, action="record-repair-review", payload=request | {"extra": True})
    receipt = record(conn, cfg, request)
    assert receipt["actor"] == "repair-owner"


def test_review_rejects_changed_digest_and_wrong_previous_review(conn, config, tmp_path):
    cfg, task, _job, _case = setup_evidence(conn, config, tmp_path)
    view = detail(conn, cfg, task)
    with pytest.raises(bugs.BugConflict, match="evidence or review changed"):
        record(conn, cfg, review_payload(view, task, digest="f" * 64))

    first = record(conn, cfg, review_payload(view, task))
    current = detail(conn, cfg, task)
    stale = review_payload(current, task, request_id="stale-review")
    stale["expected_review_id"] = None
    with pytest.raises(bugs.BugConflict, match="evidence or review changed"):
        record(conn, cfg, stale)
    assert conn.execute("SELECT count(*) FROM project_repair_reviews").fetchone()[0] == 1
    assert first["review_id"] == current["review"]["review_id"]


@pytest.mark.parametrize("mutation", ["new_command", "new_job", "new_attempt"])
def test_new_command_job_or_attempt_stales_existing_repair_decision(conn, config, tmp_path, mutation):
    cfg, task, job, _case = setup_evidence(conn, config, tmp_path)
    view = detail(conn, cfg, task)
    record(conn, cfg, review_payload(view, task))

    if mutation == "new_command":
        request_id = "later-command"
        conn.execute(
            "INSERT INTO broker_remote_actions VALUES(?,1234,'seed-grant','fixture',?,'cancelled','now','now')",
            (request_id, json.dumps({"late": True})),
        )
    elif mutation == "new_attempt":
        conn.execute("UPDATE jobs SET attempt_no=2 WHERE job_id=?", (job,))
    else:
        cfg.raw["repositories"]["linux"] = dict(cfg.raw["repositories"]["u-boot"])
        source = {**selection(cfg, "linux"), "branch": "main", "base_commit": "c" * 40, "version": "synthetic"}
        revision = conn.execute("SELECT revision FROM project_bugs WHERE bug_id=?", (task["bug_id"],)).fetchone()[0]
        continued = task | {
            "repository": "linux",
            "source": source,
            "round_id": task["round_id"],
            "expected_revision": revision,
            "request_id": "second-repository-job",
            "predecessor_job_id": job,
        }
        created = investigation.submit(conn, cfg, continued)
        conn.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (created["job_id"],))

    refreshed = detail(conn, cfg, task)
    assert refreshed["review_state"] == "stale"
    assert refreshed["repair_state"] != "ready"
    with pytest.raises(bugs.BugConflict, match="evidence or review changed"):
        record(conn, cfg, review_payload(view, task, request_id=f"stale-{mutation}"))


@pytest.mark.parametrize("evidence,untracked,expected_blocker", [
    (False, (), "current_changeset_unavailable"),
    (True, ("scratch.ignored",), "untracked_content"),
])
def test_missing_or_untracked_changeset_blocks_ready(conn, config, tmp_path, evidence, untracked, expected_blocker):
    cfg, task, _job, _case = setup_evidence(conn, config, tmp_path, evidence=evidence, untracked=untracked)
    view = detail(conn, cfg, task)
    assert view["can_mark_ready"] is False
    assert expected_blocker in view["repositories"][0]["blockers"]
    with pytest.raises(bugs.BugConflict, match="complete current repository evidence"):
        record(conn, cfg, review_payload(view, task))
    assert conn.execute("SELECT count(*) FROM project_repair_reviews").fetchone()[0] == 0


def test_second_repository_in_scope_cannot_be_omitted(conn, config, tmp_path):
    from test_project_verification import definition
    from k3_support import project_verification as plans

    cfg, task, job, _case = setup_evidence(conn, config, tmp_path)
    cfg.raw["repositories"]["linux"] = dict(cfg.raw["repositories"]["u-boot"])
    source = {**selection(cfg, "linux"), "branch": "main", "base_commit": "c" * 40, "version": "synthetic"}
    revision = conn.execute("SELECT revision FROM project_bugs WHERE bug_id=?", (task["bug_id"],)).fetchone()[0]
    created = investigation.submit(conn, cfg, task | {
        "repository": "linux", "source": source, "expected_revision": revision,
        "request_id": "linux-investigation", "predecessor_job_id": job,
    })
    conn.execute("UPDATE jobs SET state='succeeded',attempt_no=1 WHERE job_id=?", (created["job_id"],))

    plan = definition()
    plan["repositories"][0]["repository"] = "u-boot"
    plan["repositories"].append({
        "id": "repo2", "node": "build-node", "repository": "linux", "branch": "main",
        "base_commit": "c" * 40, "candidate_commit": "d" * 40,
    })
    plan["steps"][0]["repositories"].append("repo2")
    revision = conn.execute("SELECT revision FROM project_bugs WHERE bug_id=?", (task["bug_id"],)).fetchone()[0]
    plans.publish(conn, bug_id=task["bug_id"], round_id=task["round_id"], actor="repair-owner",
                  request_id="two-repository-plan", expected_revision=revision, plan=plan)

    view = detail(conn, cfg, task)
    assert {item["repository"] for item in view["repositories"]} == {"u-boot", "linux"}
    linux = next(item for item in view["repositories"] if item["repository"] == "linux")
    assert "current_changeset_unavailable" in linux["blockers"]
    assert view["can_mark_ready"] is False


def test_two_repositories_with_complete_receipts_can_be_marked_ready_and_binding_change_stales_them(conn, config, tmp_path):
    cfg, task, first_job, _case = setup_evidence(conn, config, tmp_path)
    cfg.raw["repositories"]["linux"] = dict(cfg.raw["repositories"]["u-boot"])
    source = {**selection(cfg, "linux"), "branch": "main", "base_commit": "c" * 40, "version": "synthetic"}
    revision = conn.execute("SELECT revision FROM project_bugs WHERE bug_id=?", (task["bug_id"],)).fetchone()[0]
    created = investigation.submit(conn, cfg, task | {
        "repository": "linux", "source": source, "expected_revision": revision,
        "request_id": "linux-complete-investigation", "predecessor_job_id": first_job,
    })
    conn.execute("UPDATE jobs SET state='succeeded',attempt_no=1 WHERE job_id=?", (created["job_id"],))
    settle_repository_job(conn, cfg, task, created["job_id"], head="d" * 40)

    view = detail(conn, cfg, task)
    assert view["can_mark_ready"] is True
    assert view["ready_blockers"] == []
    assert {item["repository"] for item in view["repositories"]} == {"u-boot", "linux"}
    receipt = record(conn, cfg, review_payload(view, task))
    evidence_row = conn.execute(
        "SELECT evidence_json,evidence_digest FROM project_repair_reviews WHERE review_id=?",
        (receipt["review_id"],),
    ).fetchone()
    saved = json.loads(evidence_row["evidence_json"])
    saved_repositories = {item["repository"]: item for item in saved["repositories"]}
    assert set(saved_repositories) == {"u-boot", "linux"}
    assert saved_repositories["u-boot"]["commits"] == ["b" * 40]
    assert saved_repositories["linux"]["commits"] == ["d" * 40]

    before = list(conn.execute("SELECT * FROM project_repair_reviews ORDER BY rowid"))
    cfg.raw["repositories"]["linux"]["path"] += "-changed"
    stale = detail(conn, cfg, task)
    after = list(conn.execute("SELECT * FROM project_repair_reviews ORDER BY rowid"))
    assert stale["review_state"] == "stale"
    assert stale["repair_state"] != "ready"
    assert "repository_evidence_incomplete" in stale["ready_blockers"]
    assert "no_committed_change" not in stale["ready_blockers"]
    assert stale["evidence_digest"] != evidence_row["evidence_digest"]
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_repair_review_rows_are_immutable_and_retained(conn, config, tmp_path):
    cfg, task, _job, _case = setup_evidence(conn, config, tmp_path)
    view = detail(conn, cfg, task)
    receipt = record(conn, cfg, review_payload(view, task))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE project_repair_reviews SET rationale='changed' WHERE review_id=?", (receipt["review_id"],))
    with pytest.raises(sqlite3.IntegrityError, match="retention"):
        conn.execute("DELETE FROM project_repair_reviews WHERE review_id=?", (receipt["review_id"],))
