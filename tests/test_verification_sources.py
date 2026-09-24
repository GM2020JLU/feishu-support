# ruff: noqa: F811 -- pytest injects the imported execution fixture
"""Source probes use real isolated Git repos; broker execution is synthetic."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_project_verification_runs import context, enqueue  # noqa: F401

from k3_support import project_verification as plans
from k3_support import project_verification_runs as runs
from k3_support.broker_remote_runner import run_one


@pytest.fixture
def source_context(conn, context):
    cfg, descriptor, bug, round_, prior, intent, request = context
    definition = prior["definition"]
    definition["repositories"][0]["node"] = cfg.runtime("remote_host")
    definition["steps"][0]["node"] = cfg.runtime("remote_host")
    plan = plans.publish(
        conn,
        bug_id=bug["bug_id"],
        round_id=round_["round_id"],
        actor="owner",
        request_id="source-plan",
        expected_revision=3,
        plan=definition,
    )
    intent = intent | {
        "plan_id": plan["plan_id"],
        "source_paths": {"repo": cfg.raw["repositories"]["u-boot"]["path"]},
        "config": cfg,
    }
    prepared = runs.prepare(conn, **intent)
    ctx = cfg, descriptor, bug, round_, plan, intent, request
    enqueue(conn, ctx)
    return ctx, prepared


def observation(conn, run_id, *, match=True):
    bindings = json.loads(
        conn.execute(
            "SELECT bindings_json FROM project_verification_sources WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    return {
        "exit_code": 0,
        "stdout": json.dumps(
            {
                "sources": [
                    {
                        "repository": b["repository"],
                        "path": b["path"],
                        "head": b["candidate_commit"] if match else "f" * 40,
                        "end_head": b["candidate_commit"],
                        "base_is_ancestor": True,
                        "tracked_content_matches": True,
                        "tracked_count": 1,
                        "matched": match,
                        "coverage": "tracked_source_sample",
                    }
                    for b in bindings
                ]
            }
        ),
        "stderr": "",
    }


@pytest.mark.parametrize("failure", [None, "before", "after", "unavailable"])
def test_source_preflight_blocks_wrong_source_and_after_mismatch_keeps_actual_exit(
    conn, source_context, failure
):
    ctx, prepared = source_context
    cfg, descriptor = ctx[:2]
    calls = []

    def transport(**kw):
        kw["heartbeat"]()
        phase = "command" if kw.get("keepalive") else "before" if not calls else "after"
        calls.append(phase)
        if phase == "command":
            return {"exit_code": 0, "stdout": "model says passed", "stderr": ""}
        if failure == "unavailable":
            return {"exit_code": 0, "stdout": "not json", "stderr": "private marker"}
        return observation(conn, prepared["run_id"], match=failure != phase)

    result = run_one(conn, cfg, contract_reader=lambda: descriptor, transport=transport)
    view = runs.projection(conn, ctx[4]["plan_id"])[0]
    assert (
        view["bindings_verified"] is False and view["verification_state"] == "unknown"
    )
    if failure in {"before", "unavailable"}:
        assert result["state"] == "cancelled" and calls == ["before"]
        assert view["receipt"] is None
    else:
        assert result["state"] == "succeeded" and calls == [
            "before",
            "command",
            "after",
        ]
        assert view["receipt"]["exit_code"] == 0
    expected = (
        ["matched", "matched"]
        if failure is None
        else ["matched", "mismatch"]
        if failure == "after"
        else ["mismatch"]
        if failure == "before"
        else ["unavailable"]
    )
    assert [s["state"] for s in view["source_observations"]] == expected
    assert "private marker" not in json.dumps(view)


def test_source_binding_rejects_other_paths_and_nodes_atomically(conn, context):
    cfg, _, _, _, _, intent, _ = context
    before = conn.execute("SELECT count(*) FROM project_verification_runs").fetchone()[
        0
    ]
    with pytest.raises(ValueError, match="node"):
        runs.prepare(conn, **intent, config=cfg, source_paths={"repo": "/etc"})
    assert (
        conn.execute("SELECT count(*) FROM project_verification_runs").fetchone()[0]
        == before
    )


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Synthetic")
    git("config", "user.email", "synthetic@example.invalid")
    (root / "tracked.txt").write_text("original\n")
    git("add", ".")
    git("commit", "-qm", "Synthetic baseline")
    return root, git("rev-parse", "HEAD")


def probe(root, head):
    from k3_support import verification_source_probe

    binding = {
        "repository": "fixture",
        "path": str(root),
        "base_commit": head,
        "candidate_commit": head,
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(Path(verification_source_probe.__file__).resolve()),
            json.dumps([binding]),
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        timeout=15,
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def test_native_source_sampler_detects_bytes_hidden_by_git_index(repository):
    root, head = repository
    code, result = probe(root, head)
    assert code == 0 and result["sources"][0]["matched"] is True
    # Isolated child touches only this synthetic index. The global live-command
    # guard remains unchanged; production Git/index operations are not allowed.
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import subprocess,sys;subprocess.run(['/usr/bin/git','-C',sys.argv[1],'update-index','--assume-unchanged','tracked.txt'],check=True)",
            str(root),
        ],
        check=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    (root / "tracked.txt").write_text("modified\n")
    code, result = probe(root, head)
    assert code == 0 and result["sources"][0]["tracked_content_matches"] is False
    assert result["sources"][0]["matched"] is False


def test_native_sampler_refuses_source_symlink(repository, tmp_path):
    root, head = repository
    secret = tmp_path / "outside"
    secret.write_text("unrelated-marker")
    (root / "tracked.txt").unlink()
    (root / "tracked.txt").symlink_to(secret)
    code, result = probe(root, head)
    assert code == 0 and result["sources"][0]["matched"] is False
    assert "unrelated-marker" not in json.dumps(result)


def test_native_sampler_wrong_commit_is_not_a_match(repository):
    root, head = repository
    code, result = probe(root, "f" * len(head))
    assert code != 0 and result == {"error": "source_observation_unavailable"}


def test_source_records_are_immutable(conn, source_context):
    import sqlite3

    ctx, prepared = source_context
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE project_verification_sources SET bindings_json='[]'")
    with pytest.raises(sqlite3.IntegrityError, match="retention"):
        conn.execute("DELETE FROM project_verification_sources")
    from k3_support.project_verification_sources import observe

    action = conn.execute("SELECT * FROM broker_remote_actions").fetchone()
    observe(
        conn,
        ctx[0],
        action,
        phase="before",
        heartbeat=lambda: None,
        transport=lambda **kw: observation(conn, prepared["run_id"]),
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE project_verification_observations SET state='matched'")


def test_source_path_outside_case_rolls_back_intent(conn, context):
    cfg, _, bug, round_, old, intent, _ = context
    definition = old["definition"]
    definition["repositories"][0]["node"] = cfg.runtime("remote_host")
    definition["steps"][0]["node"] = cfg.runtime("remote_host")
    plan = plans.publish(
        conn,
        bug_id=bug["bug_id"],
        round_id=round_["round_id"],
        actor="owner",
        request_id="scope",
        expected_revision=3,
        plan=definition,
    )
    with pytest.raises(ValueError, match="outside"):
        runs.prepare(
            conn,
            **(intent | {"plan_id": plan["plan_id"]}),
            config=cfg,
            source_paths={"repo": "/etc"},
        )
    assert (
        conn.execute("SELECT count(*) FROM project_verification_runs").fetchone()[0]
        == 0
    )
