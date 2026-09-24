"""Real local consumer/guardian/sandbox canary; synthetic Git and DB only.

No model, socket identity, systemd observer, production services or external
channels are exercised here. The control API uses an explicit synthetic peer.
"""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml

from k3_support.broker_claim_receipts import claim
from k3_support.broker_execution_contract import ExecutionContract
from k3_support.broker_remote import read, submit
from k3_support.broker_remote_probe import observe
from k3_support.broker_remote_runner import run_one
from k3_support.broker_start import authorize
from k3_support.config import Config, validate_config
from k3_support.db import connect, migrate
from k3_support.ids import canonical_json, digest
from k3_support.store import create_case


def verify(root):
    root = root.absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = root / "workspace"
    source = workspace / "source" / "calculator"
    worktrees = workspace / "worktrees"
    control = root / "control"
    for directory in (source, worktrees, control, root / "receipts"):
        directory.mkdir(mode=0o700, parents=True)
    (source / "calculator.py").write_text("def add(left, right):\n    return left - right\n")
    (source / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\nclass Addition(unittest.TestCase):\n"
        "    def test_positive(self): self.assertEqual(add(2, 3), 5)\n"
        "    def test_negative(self): self.assertEqual(add(-2, -3), -5)\n"
        "    def test_zero(self): self.assertEqual(add(0, 7), 7)\n")
    (source / "Makefile").write_text("build:\n\tpython3 -m py_compile calculator.py\ntest:\n\tpython3 -m unittest -v\n")
    (source / ".gitignore").write_text("__pycache__/\n")
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(control), "LANG": "C.UTF-8",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    def git(directory, *args):
        return subprocess.check_output(["/usr/bin/git", "-C", str(directory), *args],
                                       env=environment, text=True, stderr=subprocess.DEVNULL).strip()
    git(source, "init", "-q")
    git(source, "config", "user.name", "Local execution canary")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "add", ".")
    git(source, "commit", "-qm", "Seed synthetic addition defect")
    baseline = git(source, "rev-parse", "HEAD")
    baseline_test = subprocess.run(["make", "test"], cwd=source, env=environment, capture_output=True, check=False)
    assert baseline_test.returncode != 0
    example = Path(__file__).resolve().parents[1] / "config/software-project.example.yaml"
    raw = yaml.safe_load(example.read_text())
    raw["mode"] = "active"
    raw["features"]["codex"] = True
    raw["paths"] = {"data_dir": str(control), "database": str(control / "state.db")}
    raw["runtime"].update(remote_transport="local", remote_host="localhost",
                          remote_workspace_root=str(workspace), remote_source_root=str(workspace / "source"),
                          remote_worktree_root=str(worktrees), remote_receipt_directory=str(root / "receipts"))
    raw["repositories"] = {"calculator": {"path": str(source), "remote": "origin", "base_branch": "main"}}
    config = Config(validate_config(raw), control / "fixture.yaml")
    conn = connect(config.database_path)
    try:
        migrate(conn)
        case, _ = create_case(conn, title="Local transport canary", case_type="bug", severity="P3", confidence=.8)
        conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (case,))
        policy = ExecutionContract("fixture", "https://example.com", "gpt-5.6-sol", "medium", "responses", "a" * 64)
        payload = {"case_id": case, "lifecycle_round": 1, "brief": "Synthetic local coding task",
                   "repos": ["calculator"], "model": policy.model, "reasoning": policy.reasoning,
                   "context_extra": {"execution": policy.selection()}}
        stamp = datetime.now(UTC).isoformat()
        conn.execute("""INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,attempt_no,
                        available_at,created_at,updated_at,context_json)
                        VALUES('fixture',?,'codex','queued',?,0,?,?,?,'{}')""",
                     (case, digest(payload), stamp, stamp, stamp))
        conn.execute("INSERT INTO broker_inputs VALUES(?,?,?)", ("fixture", canonical_json(payload), stamp))
        peer_uid = os.geteuid() + 1
        reader = lambda: policy
        def request(method, params):
            return {"version": 1, "request_id": str(uuid4()), "method": method, "params": params}
        binding = claim(conn, config, request("claim", {"pool": "debug"}), peer_uid=peer_uid,
                        control_key=os.urandom(32), contract_reader=reader)["task"]
        authorize(conn, config, request("start", {**binding, "contract_fingerprint": policy.fingerprint}),
                  peer_uid=peer_uid, contract_reader=reader)
        # Check the real sandbox boundary before doing any edits.
        check = ("from pathlib import Path; "
                 f"assert not Path({str(control / 'state.db')!r}).exists(); "
                 f"p=Path({str(source / 'calculator.py')!r}); "
                 "exec('try:\\n p.write_text(\"unexpected\")\\nexcept OSError:\\n pass\\nelse:\\n raise AssertionError(\"source writable\")'); "
                 "print('CONTROL_HIDDEN_SOURCE_READ_ONLY')")
        commands = [shlex.join(["python3", "-c", check]),
                    shlex.join(["git", "clone", "--no-local", str(source), "calculator"]),
                    "cd calculator", "git config user.name 'Local execution canary'",
                    "git config user.email fixture@example.invalid",
                    shlex.join(["python3", "-c", "from pathlib import Path; Path('calculator.py').write_text('def add(left, right):\\n    return left + right\\n'.replace('\\\\n', '\\n'))"]),
                    "make build", "make test", "git diff --check", "git add calculator.py",
                    "git commit -m 'Fix addition through local broker sandbox'", "git status --porcelain"]
        operation = request("remote_submit", {**binding, "remote": {"mode": "work", "repo": "calculator", "command": " && ".join(commands)}})
        submit(conn, config, operation, peer_uid=peer_uid, contract_reader=reader)
        result = run_one(conn, config, contract_reader=reader)
        receipt = observe(conn, config, request_id=operation["request_id"])
        output = read(conn, config, request("remote_read", {**binding, "remote_request_id": operation["request_id"], "offset": 0}),
                      peer_uid=peer_uid, contract_reader=reader)
        repo = worktrees / case / "calculator"
        report = {"scope": "real local broker consumer, guardian, sandbox and receipt; synthetic control caller",
                  "systemd_verified": False, "native_model_verified": False, "peer_socket_verified": False,
                  "baseline_tests_failed": baseline_test.returncode != 0,
                  "consumer": result, "receipt": receipt, "operation_exit": output["exit_code"],
                  "sandbox_boundary_marker": "CONTROL_HIDDEN_SOURCE_READ_ONLY" in output["stdout"],
                  "source_unchanged": git(source, "rev-parse", "HEAD") == baseline and not git(source, "status", "--porcelain")}
        if result["state"] == "succeeded":
            report.update(commit=git(repo, "rev-parse", "HEAD"), changed_files=git(repo, "diff", "--name-only", baseline, "HEAD").splitlines(),
                          new_commit_count=int(git(repo, "rev-list", "--count", baseline + "..HEAD")), clean=not git(repo, "status", "--porcelain"))
            bundle = root / "coding.bundle"
            git(repo, "bundle", "create", str(bundle), "--all")
            git(repo, "bundle", "verify", str(bundle))
            report["bundle_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
        else:
            report["synthetic_stderr"] = output["stderr"]
        report["ok"] = (result["state"] == "succeeded" and receipt.get("guard_exit_code") == 0
                        and report["sandbox_boundary_marker"] and report["source_unchanged"]
                        and report.get("changed_files") == ["calculator.py"] and report.get("new_commit_count") == 1
                        and report.get("clean") is True)
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New isolated directory outside /tmp and system roots")
    args = parser.parse_args()
    result = verify(args.output)
    print(json.dumps(result))
    raise SystemExit(0 if result["ok"] else 1)
