from __future__ import annotations

import copy
import shlex
import subprocess

import pytest

from k3_support.approvals import (
    decide_approval,
    expiry_after,
    normalized_push_action,
    request_approval,
)
from k3_support.config import Config, validate_config
from k3_support.executors import (
    ExecutionResult,
    ExecutorError,
    bind_wip_action,
    execute_wip_push,
    validate_wip_action,
)
from k3_support.store import create_case

BASE = "1" * 40
FIRST = "a" * 40
TIP = "b" * 40
URL = "ssh://developer@gerrit.example.test:29418/firmware/uboot"


class GitSnapshot:
    """Stateful fake transport: independent preview, preflight and network time."""

    def __init__(self):
        self.url = URL
        self.base = BASE
        self.head = TIP
        self.history = f"{FIRST} {BASE}\n{TIP} {FIRST}\n"
        self.calls = []
        self.pushes = []
        self.after_history = None
        self.push_error = None

    def __call__(self, argv, cwd, timeout):
        self.calls.append(argv)
        parts = shlex.split(argv[-1])
        args = parts[3:]
        if args == ["rev-parse", "--show-toplevel"]:
            value = parts[2]
        elif args[:2] == ["remote", "get-url"]:
            value = self.url
        elif args[0] == "ls-remote":
            value = f"{self.base}\t{args[-1]}"
        elif args[0] == "rev-parse":
            value = self.head
        elif args[0] == "rev-list":
            value = self.history
            if self.after_history:
                self.after_history()
        elif args[0] == "push":
            self.pushes.append(parts)
            if self.push_error:
                raise self.push_error
            value = "accepted"
        else:
            raise AssertionError(f"unexpected read-only Git command: {parts}")
        return ExecutionResult(argv, 0, value + "\n", "")


@pytest.fixture
def pending_push(conn, config):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"]["wip_push"] = True
    raw["repositories"] = {
        "u-boot": {
            "path": raw["runtime"]["remote_source_root"] + "/u-boot",
            "remote": "origin",
            "base_branch": "main",
        }
    }
    cfg = Config(validate_config(raw), config.path)
    case_id, _ = create_case(
        conn, title="synthetic push", case_type="bug", severity="P2", confidence=0.9
    )
    conn.execute("UPDATE cases SET state='waiting_push' WHERE case_id=?", (case_id,))
    action = normalized_push_action(
        case_id=case_id,
        repo="u-boot",
        destination="refs/for/main%wip",
        commits=[FIRST, TIP],
        command=["git", "push", "origin", "HEAD:refs/for/main%wip"],
        worktree=f"{cfg.runtime('remote_worktree_root')}/{case_id}/u-boot",
    )
    return cfg, case_id, action


def approve(conn, cfg, case_id, action):
    approval_id, action_digest, _ = request_approval(
        conn,
        approval_type="wip_push",
        case_id=case_id,
        action=action,
        expires_at=expiry_after(30),
    )
    decide_approval(
        conn,
        cfg,
        approval_id=approval_id,
        approve=True,
        approver_user_id="owner-user",
        approver_chat_id="owner-chat",
        message_id="synthetic-approval",
        decision_text="approve push",
        expected_digest=action_digest,
    )
    return approval_id


def verified(action, result):
    return {"change": "1", "revision": TIP, "patch_set": 1, "wip": True}


def test_preview_binds_actual_remote_base_and_complete_range(pending_push):
    cfg, _, request = pending_push
    transport = GitSnapshot()
    action = bind_wip_action(cfg, request, runner=transport)
    assert action["binding"] == {
        "version": 1,
        "remote_url": URL,
        "project": "firmware/uboot",
        "destination_branch": "main",
        "base_sha": BASE,
        "tip_sha": TIP,
    }
    assert transport.pushes == []
    assert "binding" not in request


def test_moved_head_and_remote_after_preflight_cannot_change_network_target(
    conn, pending_push
):
    cfg, case_id, request = pending_push
    transport = GitSnapshot()
    action = bind_wip_action(cfg, request, runner=transport)
    approval_id = approve(conn, cfg, case_id, action)

    def move_refs():
        transport.head = "f" * 40
        transport.url = "ssh://other@gerrit.example.test:29418/elsewhere"

    transport.after_history = move_refs
    execute_wip_push(
        conn, cfg, case_id=case_id, action=action, runner=transport, verifier=verified
    )
    assert transport.pushes[0][-5:] == [
        "push",
        "-o",
        "wip",
        URL,
        f"{TIP}:refs/for/main",
    ]
    assert "HEAD" not in shlex.join(transport.pushes[0])
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()[0]
        == "consumed"
    )


@pytest.mark.parametrize("mutation", ["remote", "base", "head"])
def test_changed_preview_inputs_require_fresh_approval(conn, pending_push, mutation):
    cfg, case_id, request = pending_push
    transport = GitSnapshot()
    action = bind_wip_action(cfg, request, runner=transport)
    approval_id = approve(conn, cfg, case_id, action)
    if mutation == "remote":
        transport.url = "ssh://other@gerrit.example.test:29418/other/project"
    elif mutation == "base":
        transport.base = "2" * 40
        transport.history = f"{FIRST} {transport.base}\n{TIP} {FIRST}"
    else:
        transport.head = "c" * 40
    with pytest.raises(ExecutorError):
        execute_wip_push(
            conn,
            cfg,
            case_id=case_id,
            action=action,
            runner=transport,
            verifier=verified,
        )
    assert transport.pushes == []
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()[0]
        == "approved"
    )


@pytest.mark.parametrize(
    "commits", [[TIP], ["c" * 40, FIRST, TIP], [TIP, FIRST, TIP], [TIP, FIRST]]
)
def test_hidden_extra_duplicate_or_reordered_commits_are_rejected(
    pending_push, commits
):
    cfg, _, request = pending_push
    request["commits"] = commits
    with pytest.raises(ExecutorError):
        bind_wip_action(cfg, request, runner=GitSnapshot())


def test_merge_history_and_multiple_push_urls_are_rejected(pending_push):
    cfg, _, request = pending_push
    transport = GitSnapshot()
    transport.history = f"{FIRST} {BASE}\n{TIP} {FIRST} {'c' * 40}"
    with pytest.raises(ExecutorError, match="merge"):
        bind_wip_action(cfg, request, runner=transport)
    transport.url = URL + "\n" + URL
    with pytest.raises(ExecutorError, match="one exact SSH URL"):
        bind_wip_action(cfg, request, runner=transport)


@pytest.mark.parametrize(
    "option", ["ready", "submit", "notify=ALL", "r=someone", "wip"]
)
def test_unapproved_destination_options_and_duplicates_are_rejected(
    pending_push, option
):
    cfg, _, request = pending_push
    request["destination"] += "," + option
    request["command"][-1] += "," + option
    with pytest.raises(ExecutorError, match="only the Gerrit wip destination"):
        bind_wip_action(cfg, request, runner=GitSnapshot())


def test_legacy_approval_is_rejected_without_any_remote_call(conn, pending_push):
    cfg, case_id, request = pending_push
    approve(conn, cfg, case_id, request)
    transport = GitSnapshot()
    with pytest.raises(ExecutorError, match="fresh preview"):
        execute_wip_push(conn, cfg, case_id=case_id, action=request, runner=transport)
    assert transport.calls == []


@pytest.mark.parametrize("revoke", ["case", "job", "approval"])
def test_revocation_during_preflight_prevents_push(conn, pending_push, revoke):
    cfg, case_id, request = pending_push
    transport = GitSnapshot()
    action = bind_wip_action(cfg, request, runner=transport)
    approval_id = approve(conn, cfg, case_id, action)
    kwargs = {}
    if revoke == "job":
        from k3_support.ids import digest
        from k3_support.timeutil import iso_now

        now = iso_now()
        conn.execute(
            """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,attempt_no,max_attempts,
            lease_owner,lease_expires_at,available_at,created_at,updated_at)
            VALUES('push-job',?,'push','running',?,1,1,'worker',?,?,?,?)""",
            (case_id, digest(action), expiry_after(30), now, now, now),
        )
        kwargs = {"job_id": "push-job", "lease_owner": "worker", "attempt_no": 1}

    def revoke_authority():
        if revoke == "case":
            conn.execute(
                "UPDATE cases SET state='takeover' WHERE case_id=?", (case_id,)
            )
        elif revoke == "job":
            conn.execute("UPDATE jobs SET state='cancelled' WHERE job_id='push-job'")
        else:
            conn.execute(
                "UPDATE approvals SET status='revoked' WHERE approval_id=?",
                (approval_id,),
            )

    transport.after_history = revoke_authority
    with pytest.raises(Exception, match="Case|claim|approval changed"):
        execute_wip_push(
            conn,
            cfg,
            case_id=case_id,
            action=action,
            runner=transport,
            verifier=verified,
            **kwargs,
        )
    assert transport.pushes == []
    assert (
        conn.execute(
            "SELECT count(*) FROM action_ledger WHERE action_type='push'"
        ).fetchone()[0]
        == 0
    )


def test_transport_timeout_is_durable_uncertain_and_cannot_retry(conn, pending_push):
    cfg, case_id, request = pending_push
    transport = GitSnapshot()
    action = bind_wip_action(cfg, request, runner=transport)
    approve(conn, cfg, case_id, action)
    transport.push_error = subprocess.TimeoutExpired("synthetic git push", 300)
    with pytest.raises(ExecutorError, match="uncertain"):
        execute_wip_push(conn, cfg, case_id=case_id, action=action, runner=transport)
    assert (
        conn.execute(
            "SELECT state FROM action_ledger WHERE action_type='push'"
        ).fetchone()[0]
        == "uncertain"
    )
    with pytest.raises(Exception, match="unconsumed"):
        execute_wip_push(conn, cfg, case_id=case_id, action=action, runner=transport)
    assert len(transport.pushes) == 1


def test_bound_destination_and_tip_cannot_be_changed(pending_push):
    cfg, _, request = pending_push
    action = bind_wip_action(cfg, request, runner=GitSnapshot())
    action["binding"]["destination_branch"] = "other"
    with pytest.raises(ExecutorError, match="destination mismatch"):
        validate_wip_action(action, cfg)


def test_preview_commands_verify_real_local_git_history_without_network(
    pending_push, tmp_path
):
    cfg, _, request = pending_push
    checkout = tmp_path / "git-fixture"
    checkout.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Synthetic fixture")
    git("config", "user.email", "fixture@example.test")
    git("config", "commit.gpgsign", "false")
    git("commit", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-b", "fix")
    git("commit", "--allow-empty", "-m", "first fix")
    first = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "second fix")
    tip = git("rev-parse", "HEAD")
    git("remote", "add", "origin", URL)
    request["commits"] = [first, tip]

    def local_read_transport(argv, cwd, timeout):
        args = shlex.split(argv[-1])[3:]
        assert args[0] in {"rev-parse", "remote", "ls-remote", "rev-list"}
        if args == ["rev-parse", "--show-toplevel"]:
            output = request["worktree"]
        else:
            if args[0] == "ls-remote":
                args[args.index(URL)] = str(checkout)
            output = git(*args)
        return ExecutionResult(argv, 0, output, "")

    bound = bind_wip_action(cfg, request, runner=local_read_transport)
    assert bound["binding"]["base_sha"] == base
    assert bound["binding"]["tip_sha"] == tip
    assert bound["commits"] == [first, tip]
    git("checkout", "--orphan", "other-root")
    git("commit", "--allow-empty", "-m", "unrelated history")
    git("checkout", "fix")
    git("merge", "--no-ff", "--allow-unrelated-histories", "other-root", "-m", "merge")
    request["commits"] = git(
        "rev-list", "--reverse", "--topo-order", f"{base}..HEAD"
    ).splitlines()
    with pytest.raises(ExecutorError, match="linear range"):
        bind_wip_action(cfg, request, runner=local_read_transport)
