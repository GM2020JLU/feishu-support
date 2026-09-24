"""Exercise a joint verifier through its broker-prepared, two-repository workspace.

The fixture transport unwraps the local heartbeat guardian and bwrap renderer,
then executes the actual control-generated Python payloads and source samplers
locally. It proves command/workspace/evidence integration, not bwrap isolation.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import shlex
from uuid import uuid4

import pytest

from test_coding_catalog import choices
from test_project_verification import definition
from test_project_verifier_job import setup as verifier_setup

from k3_support import project_verifier_job as verifier
from k3_support import project_verification as plans
from k3_support import project_verification_reviews as reviews
from k3_support import project_verification_runs as runs
from k3_support.broker_claim_receipts import claim
from k3_support.broker_remote import submit
from k3_support.broker_remote_runner import run_one
from k3_support.broker_start import authorize
from k3_support.coding_catalog import resolve
from k3_support.config import Config, validate_config
from k3_support.project_investigation_source import selection as source_selection


BUILD_ROOT = Path.home() / ".local/share/codex/builds/feishu-support/joint-verification-20260920"
PRIMARY = "u-boot"
COMPANION = "companion-firmware"
UID = os.geteuid() + 1


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()


def make_repository(path, name):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(path)], check=True)
    git(path, "config", "user.email", "joint-test@example.invalid")
    git(path, "config", "user.name", "Joint verifier test")
    version = path / "candidate.txt"
    version.write_text(name + "-base\n")
    git(path, "add", "candidate.txt")
    git(path, "commit", "-qm", "base")
    base = git(path, "rev-parse", "HEAD")
    version.write_text(name + "-candidate\n")
    git(path, "commit", "-qam", "candidate")
    candidate = git(path, "rev-parse", "HEAD")
    return base, candidate


@pytest.fixture
def joint_case(conn, config, tmp_path):
    # Keep actual repositories and bwrap workspaces in a task-owned scratch root.
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="codex-joint-integration-", dir=BUILD_ROOT))
    try:
        cfg, request, _parent, _case = verifier_setup(conn, config, tmp_path)
        remote_root = scratch / "remote"
        source_root = remote_root / "source"
        worktree_root = remote_root / "worktrees"
        primary_path = source_root / PRIMARY
        companion_path = source_root / COMPANION
        primary_base, primary_candidate = make_repository(primary_path, PRIMARY)
        companion_base, companion_candidate = make_repository(companion_path, COMPANION)
        remote_root.mkdir(parents=True, exist_ok=True)
        worktree_root.mkdir()

        cfg.raw["repositories"][PRIMARY].update(
            path=str(primary_path), remote="origin", base_branch="main"
        )
        cfg.raw["repositories"][COMPANION] = {
            "path": str(companion_path), "remote": "origin", "base_branch": "main"
        }
        cfg.raw["runtime"].update(
            remote_transport="local", remote_host="localhost",
            remote_workspace_root=str(remote_root),
            remote_source_root=str(source_root),
            remote_worktree_root=str(worktree_root),
            remote_toolchain_roots=[], remote_receipt_directory=None,
        )
        cfg = Config(validate_config(cfg.raw), cfg.path)

        primary_source = {
            **source_selection(cfg, PRIMARY), "branch": "main",
            "base_commit": primary_candidate, "version": "joint-candidate",
        }
        companion_source = {
            **source_selection(cfg, COMPANION), "branch": "main",
            "base_commit": companion_candidate, "version": "joint-candidate",
        }
        spec = definition()
        first = spec["repositories"][0]
        first.update(
            repository=PRIMARY, branch="main", node=cfg.runtime("remote_host"),
            base_commit=primary_base, candidate_commit=primary_candidate,
        )
        spec["repositories"].append({
            "id": "companion", "repository": COMPANION, "branch": "main",
            "node": cfg.runtime("remote_host"), "base_commit": companion_base,
            "candidate_commit": companion_candidate,
        })
        step = spec["steps"][0]
        step.update(
            layer="software_test", node=cfg.runtime("remote_host"),
            repositories=["repo", "companion"], artifacts=[], devices=[],
        )
        revision = conn.execute(
            "SELECT revision FROM project_bugs WHERE bug_id=?", (request["bug_id"],)
        ).fetchone()[0]
        plan = plans.publish(
            conn, bug_id=request["bug_id"], round_id=request["round_id"],
            actor=cfg.control_operator_id, request_id="joint-real-repositories",
            expected_revision=revision, plan=spec,
        )
        request["source"] = primary_source
        request["expected_revision"] = revision + 1
        request["verification"] = {
            "plan_id": plan["plan_id"], "step_id": "function",
            "command": (
                "python3 -c " + shlex.quote(
                    "import json,os; from pathlib import Path; "
                    "s=json.loads(os.environ['K3_VERIFICATION_SOURCES']); "
                    "v={k:(Path(p)/'candidate.txt').read_text().strip() for k,p in s.items()}; "
                    "assert v=={'u-boot':'u-boot-candidate','companion-firmware':'companion-firmware-candidate'}; "
                    "print('JOINT_TEST_EVIDENCE='+json.dumps(v,sort_keys=True))"
                )
            ),
            "sources": {PRIMARY: primary_source, COMPANION: companion_source},
        }
        # These are normal control-generated commands; only the transport is local.
        yield cfg, request, scratch, worktree_root
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _broker_transport(conn, calls):
    def transport(**kwargs):
        kwargs["heartbeat"]()
        argv = kwargs["argv"]
        if argv[:3] == ["/bin/bash", "--noprofile", "--norc"]:
            tokens = shlex.split(argv[-1])
        else:
            tokens = list(argv)
        if len(tokens) >= 6 and tokens[:4] == ["/usr/bin/python3", "-I", "-S", "-c"] \
                and "def supervise(command)" in tokens[4]:
            # Omit only the long-lived local lease/heartbeat guardian.
            rendered = tokens[5]
            # The renderer appends the exact sandbox argv after its shell checks.
            rendered_argv = shlex.split(rendered.rpartition(" && exec ")[2])
            command = rendered_argv[rendered_argv.index("--") + 5]
            cwd = rendered_argv[rendered_argv.index("--chdir") + 1]
        elif "--" in tokens:
            command = tokens[tokens.index("--") + 5]
            cwd = kwargs["cwd"]
        else:
            command = shlex.join(tokens)
            cwd = kwargs["cwd"]
        command_argv = shlex.split(command)
        calls.append({"keepalive": bool(kwargs.get("keepalive")), "command": command})
        Path(cwd).mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            command_argv,
            cwd=cwd, env={**os.environ, **kwargs.get("env", {})},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=kwargs["timeout"], check=False,
        )
        kwargs["heartbeat"]()
        return {"exit_code": result.returncode, "stdout": result.stdout,
                "stderr": result.stderr}
    return transport


def _launch_and_submit(conn, cfg, request):
    job = verifier.submit(conn, cfg, request)
    descriptor = resolve(cfg, "primary", expected_fingerprint=choices(cfg)["items"][0]["contract_fingerprint"])
    claimed = claim(
        conn, cfg,
        {"version": 1, "request_id": str(uuid4()),
         "method": "claim", "params": {"pool": "debug"}},
        peer_uid=UID, control_key=b"j" * 32,
        contract_reader=lambda: descriptor,
    )["task"]
    assert claimed["job_id"] == job["job_id"]
    start_request = {
        "version": 1, "request_id": str(uuid4()),
        "method": "start",
        "params": claimed | {"contract_fingerprint": descriptor.fingerprint},
    }
    authorize(conn, cfg, start_request, peer_uid=UID, contract_reader=lambda: descriptor)
    page = runs.read_for_worker(
        conn, cfg,
        {"version": 1, "request_id": str(uuid4()), "method": "verification_list",
         "params": claimed | {"after_id": ""}},
        peer_uid=UID, contract_reader=lambda: descriptor,
    )
    intent = page["items"][0]
    remote_request = {
        "version": 1, "request_id": intent["remote_request_id"],
        "method": "remote_submit",
        "params": claimed | {"remote": intent["remote"]},
    }
    assert submit(conn, cfg, remote_request, peer_uid=UID,
                  contract_reader=lambda: descriptor)["accepted"]
    return job, descriptor, claimed, intent


@pytest.mark.parametrize("companion_change", [None, "changed", "missing"])
def test_joint_broker_command_reads_both_candidates_and_requires_review(
    conn, config, tmp_path, joint_case, companion_change
):
    cfg, request, _scratch, _worktrees = joint_case
    calls = []
    job, descriptor, _task, intent = _launch_and_submit(conn, cfg, request)
    transport = _broker_transport(conn, calls)
    test_commands = lambda: [item for item in calls if "JOINT_TEST_EVIDENCE" in item["command"]]

    # The workspace action must run and record its receipt before the test action.
    prepared = run_one(conn, cfg, contract_reader=lambda: descriptor, transport=transport)
    assert prepared["state"] == "succeeded", (prepared, calls)
    checkout = json.loads(conn.execute(
        "SELECT plan_json FROM broker_remote_actions WHERE request_id=?",
        (prepared["request_id"],),
    ).fetchone()[0])["investigation_checkout"]
    companion_path = Path(checkout["root"]) / checkout["companions"][0]["relative_path"]
    if companion_change == "changed":
        (companion_path / "candidate.txt").write_text("changed-after-preparation\n")
    elif companion_change == "missing":
        shutil.rmtree(companion_path)

    result = run_one(conn, cfg, contract_reader=lambda: descriptor, transport=transport)
    run = conn.execute(
        "SELECT * FROM project_verification_runs WHERE remote_request_id=?",
        (intent["remote_request_id"],),
    ).fetchone()
    view = runs.projection(conn, request["verification"]["plan_id"])[0]
    assessment = reviews.assessments(conn, request["verification"]["plan_id"])[0]

    if companion_change is None:
        assert result["state"] == "succeeded"
        remote = conn.execute(
            "SELECT stdout,exit_code FROM broker_remote_results WHERE request_id=?",
            (intent["remote_request_id"],),
        ).fetchone()
        assert remote["exit_code"] == 0
        assert "JOINT_TEST_EVIDENCE={\"companion-firmware\": \"companion-firmware-candidate\", \"u-boot\": \"u-boot-candidate\"}" in remote["stdout"]
        assert len(test_commands()) == 1
        assert test_commands()[0]["keepalive"] is True
        observations = view["source_observations"]
        assert {item["phase"] for item in observations} == {"before", "after"}
        assert all(item["state"] == "matched" for item in observations)
        assert all(
            {source["repository"] for source in item["sources"]} == {PRIMARY, COMPANION}
            for item in observations
        )
        assert view["verification_state"] == "unknown"
        assert view["bindings_verified"] is False and view["oracle_verified"] is False
        assert assessment["state"] == "unknown"
        assert conn.execute(
            "SELECT count(*) FROM project_verification_reviews WHERE run_id=?", (run["run_id"],)
        ).fetchone()[0] == 0
    else:
        assert result["state"] == "cancelled"
        assert not test_commands()
        assert conn.execute(
            "SELECT 1 FROM broker_remote_results WHERE request_id=?",
            (intent["remote_request_id"],),
        ).fetchone() is None
        assert assessment["state"] == "unknown"
        assert "source_not_verified" in assessment["pass_blockers"]
        assert run is not None
        from k3_support.project_bugs import BugConflict
        with pytest.raises(BugConflict, match="verification pass requires complete bound evidence"):
            reviews.record(
                conn, actor=cfg.control_operator_id,
                payload={
                    "run_id": run["run_id"], "request_id": "blocked-joint-pass",
                    "evidence_digest": assessment["evidence_digest"],
                    "expected_review_id": None, "verdict": "passed",
                    "rationale": "Attempted pass without second-source evidence.",
                    "attested": True,
                },
            )

    assert conn.execute("SELECT count(*) FROM project_verification_runs WHERE grant_id=?",
                        (run["grant_id"],)).fetchone()[0] == 1


def test_joint_zero_exit_cannot_pass_when_command_changes_companion(conn, joint_case):
    cfg, request, _scratch, _worktrees = joint_case
    request['verification']['command'] = 'python3 -c ' + shlex.quote(
        "import json,os; from pathlib import Path; "
        "s=json.loads(os.environ['K3_VERIFICATION_SOURCES']); "
        "(Path(s['companion-firmware'])/'candidate.txt').write_text('changed during test\\n')"
    )
    calls = []
    _job, descriptor, _task, intent = _launch_and_submit(conn, cfg, request)
    transport = _broker_transport(conn, calls)
    assert run_one(conn, cfg, contract_reader=lambda: descriptor, transport=transport)['state'] == 'succeeded'
    run_one(conn, cfg, contract_reader=lambda: descriptor, transport=transport)
    remote = conn.execute('SELECT exit_code FROM broker_remote_results WHERE request_id=?',
                          (intent['remote_request_id'],)).fetchone()
    assert remote['exit_code'] == 0
    view = runs.projection(conn, request['verification']['plan_id'])[0]
    states = {item['phase']: item['state'] for item in view['source_observations']}
    assert states['before'] == 'matched'
    assert states['after'] != 'matched'
    assessment = reviews.assessments(conn, request['verification']['plan_id'])[0]
    assert assessment['state'] != 'passed'
    assert 'source_not_verified' in assessment['pass_blockers']
    from k3_support.project_bugs import BugConflict
    with pytest.raises(BugConflict, match='verification pass requires complete bound evidence'):
        reviews.record(conn, actor=cfg.control_operator_id, payload={
            'run_id': view['run_id'], 'request_id': 'changed-during-joint-pass',
            'evidence_digest': assessment['evidence_digest'], 'expected_review_id': None,
            'verdict': 'passed', 'rationale': 'Exit zero must not override changed source evidence.',
            'attested': True,
        })
