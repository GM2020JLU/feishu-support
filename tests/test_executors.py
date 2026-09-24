from __future__ import annotations

import copy
import json
import shutil
import subprocess

import pytest

from k3_support.approvals import (
    ApprovalError,
    decide_approval,
    expiry_after,
    normalized_board_action,
    normalized_push_action,
    request_approval,
)
from k3_support.config import Config, validate_config
from k3_support.control import ControlMessage, execute_control
from k3_support.executors import (
    BoardExecutor,
    ExecutionResult,
    ExecutorError,
    _render_codex_policy,
    create_codex_job,
    execute_wip_push,
    record_codex_result,
    run_codex_job,
    validate_codex_result_text,
    validate_wip_action,
    verify_gerrit_wip,
)
from k3_support.store import create_case, transition_case


def executor_config(config, **features):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"].update(features)
    raw["repositories"] = {
        "u-boot": {
            "path": "/data/home2/operator/WorkSpace/k3/uboot-2022.10",
            "remote": "origin",
            "base_branch": "main",
        }
    }
    return Config(validate_config(raw), config.path)


def approved_board(conn, config, case_id, session="s1"):
    action = normalized_board_action(case_id, session, 45)
    approval_id, action_digest, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id=session,
        action=action,
        expires_at=expiry_after(60),
    )
    decide_approval(
        conn,
        config,
        approval_id=approval_id,
        approve=True,
        approver_user_id="owner-user",
        approver_chat_id="owner-chat",
        message_id="tg-board",
        decision_text="approve board",
        expected_digest=action_digest,
    )
    return approval_id


def test_codex_job_uses_fixed_model_and_reasoning(conn, config):
    cfg = executor_config(config, codex=True)
    case_id, _ = create_case(
        conn, title="bug", case_type="bug", severity="P2", confidence=0.2
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="classified",
        expected_version=1,
    )
    brief = "# UNTRUSTED INPUT\nmessage\n# FORBIDDEN ACTIONS\nNo push\n# ACCEPTANCE TESTS\nBuild"
    job_id, created = create_codex_job(
        conn, cfg, case_id=case_id, brief=brief, repo="u-boot"
    )
    assert created
    calls = []

    def runner(argv, cwd, timeout):
        calls.append((argv, cwd, timeout))
        return ExecutionResult(argv, 0, "done", "")

    run_codex_job(conn, cfg, job_id=job_id, runner=runner)
    argv = calls[0][0]
    assert argv[argv.index("-m") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="medium"' in argv
    assert 'approval_policy="never"' in argv
    assert "workspace-write" in argv
    assert (
        conn.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        == "succeeded"
    )
    attempt = conn.execute(
        "SELECT result,ended_at FROM job_attempts WHERE job_id=?", (job_id,)
    ).fetchone()
    assert attempt["result"] == "succeeded"
    assert attempt["ended_at"] is not None


def test_takeover_fences_codex_result_that_finishes_after_cancellation(conn, config):
    cfg = executor_config(config, codex=True)
    case_id, _ = create_case(
        conn, title="race", case_type="bug", severity="P2", confidence=0.5
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id="test",
        reason="classified",
        expected_version=1,
    )
    brief = (
        "# UNTRUSTED INPUT\nrace\n# FORBIDDEN ACTIONS\nNo push"
        "\n# ACCEPTANCE TESTS\nBuild"
    )
    job_id, _ = create_codex_job(
        conn, cfg, case_id=case_id, brief=brief, repo="u-boot"
    )

    def runner(argv, cwd, timeout):
        execute_control(
            conn,
            cfg,
            ControlMessage(
                "owner-user",
                "owner-chat",
                "takeover-during-codex",
                f"takeover {case_id} 2",
            ),
        )
        return ExecutionResult(argv, 0, "late success", "")

    with pytest.raises(ExecutorError, match="cancelled or lost its lease"):
        run_codex_job(conn, cfg, job_id=job_id, runner=runner)
    job = conn.execute(
        "SELECT state,output_digest FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    assert tuple(job) == ("cancelled", None)
    attempt = conn.execute(
        "SELECT result,ended_at FROM job_attempts WHERE job_id=?", (job_id,)
    ).fetchone()
    assert attempt["result"] == "cancelled"
    assert attempt["ended_at"] is not None


def test_codex_policy_is_rendered_from_portable_runtime(config):
    raw = copy.deepcopy(config.raw)
    raw["runtime"].update(
        {
            "ssh_command": "/opt/ssh/bin/ssh",
            "serial_command": "/opt/board/bin/serial",
            "board_control_script": "/opt/board/board-ctrl.sh",
            "board_boot_script": "/opt/board/ram-boot.sh",
            "codex_remote_command": "/opt/k3/bin/k3-codex-remote",
            "codex_board_command": "/opt/k3/bin/k3-codex-board",
        }
    )
    cfg = Config(validate_config(raw), config.path)
    policy = _render_codex_policy(cfg)
    assert '["/opt/k3/bin/k3-codex-remote"]' in policy
    assert '["/opt/k3/bin/k3-codex-board"]' in policy
    assert '["/opt/ssh/bin/ssh"]' in policy
    assert '["bash", "/opt/board/board-ctrl.sh"]' in policy
    assert '["/opt/board/board-ctrl.sh"]' in policy
    assert "/home/operator" not in policy


def test_rendered_codex_policy_parses_and_enforces_wrapper_boundary(
    config, tmp_path
):
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("Codex execpolicy checker is unavailable")
    raw = copy.deepcopy(config.raw)
    raw["runtime"].update(
        {
            "codex_remote_command": "/opt/k3/bin/k3-codex-remote",
            "codex_board_command": "/opt/k3/bin/k3-codex-board",
        }
    )
    cfg = Config(validate_config(raw), config.path)
    rules = tmp_path / "k3-support.rules"
    rules.write_text(_render_codex_policy(cfg), encoding="utf-8")

    def decision(command):
        result = subprocess.run(
            [codex, "execpolicy", "check", "--rules", str(rules), *command],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)["decision"]

    assert decision(["/opt/k3/bin/k3-codex-remote", "case", "inspect"]) == "allow"
    assert decision(["ssh", "build-host", "hostname"]) == "forbidden"
    assert decision(["git", "push", "origin", "HEAD:refs/for/main%wip"]) == "forbidden"


def test_codex_result_schema_requires_all_non_empty_sections():
    value = "\n".join(
        f"## {name}\n{('completed' if name == 'status' else name + ' value')}"
        for name in (
            "status",
            "root_cause",
            "changes",
            "verification",
            "board_state",
            "push_state",
            "artifacts",
            "risks",
            "next_action",
            "reply_draft",
        )
    )
    assert validate_codex_result_text(value)["status"] == "completed"
    with pytest.raises(ExecutorError, match="reply_draft"):
        validate_codex_result_text(value.rsplit("## reply_draft", 1)[0])


def test_succeeded_codex_result_is_recorded_once_as_shadow_suggestion(conn, config):
    cfg = executor_config(config, codex=True)
    case_id, _ = create_case(
        conn, title="bug", case_type="bug", severity="P2", confidence=0.2
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="classified",
        expected_version=1,
    )
    transition_case(
        conn,
        case_id=case_id,
        after="investigating",
        actor_type="system",
        actor_id=None,
        reason="delegate",
        expected_version=2,
    )
    brief = (
        "# UNTRUSTED INPUT\nx\n# FORBIDDEN ACTIONS\nNo push\n# ACCEPTANCE TESTS\nBuild"
    )
    job_id, _ = create_codex_job(conn, cfg, case_id=case_id, brief=brief, repo="u-boot")
    run_codex_job(
        conn,
        cfg,
        job_id=job_id,
        runner=lambda argv, cwd, timeout: ExecutionResult(argv, 0, "done", ""),
    )
    row = conn.execute("SELECT workdir FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    value = "\n".join(
        f"## {name}\n{('completed' if name == 'status' else name + ' value')}"
        for name in (
            "status",
            "root_cause",
            "changes",
            "verification",
            "board_state",
            "push_state",
            "artifacts",
            "risks",
            "next_action",
            "reply_draft",
        )
    )
    (__import__("pathlib").Path(row["workdir"]) / "codex-final.md").write_text(
        value, encoding="utf-8"
    )
    assert record_codex_result(conn, job_id=job_id)["status"] == "completed"
    assert record_codex_result(conn, job_id=job_id)["status"] == "completed"
    assert (
        conn.execute(
            "SELECT count(*) FROM case_events WHERE event_type='codex_completed'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM case_suggestions WHERE kind='reply_draft'"
        ).fetchone()[0]
        == 1
    )


def test_board_actions_require_one_lease_then_run_without_per_action_approval(
    conn, config
):
    cfg = executor_config(config, board=True)
    case_id, _ = create_case(
        conn, title="bug", case_type="bug", severity="P2", confidence=0.2
    )
    calls = []

    def runner(argv, cwd, timeout):
        calls.append(argv)
        return ExecutionResult(argv, 0, "fresh marker", "")

    board = BoardExecutor(cfg, runner=runner)
    with pytest.raises(ApprovalError):
        board.execute(conn, case_id=case_id, session_id="s1", action={"type": "reset"})
    approval_id = approved_board(conn, cfg, case_id)
    board.execute(conn, case_id=case_id, session_id="s1", action={"type": "reset"})
    board.execute(
        conn,
        case_id=case_id,
        session_id="s1",
        action={"type": "serial_wait", "regex": "U-Boot>", "timeout": 10},
    )
    assert len(calls) == 2
    board.close_session(conn, case_id=case_id, session_id="s1")
    assert calls[-2][-2:] == ["board1", "fastboot"]
    assert "--after" in calls[-1] and "now" in calls[-1]
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()[0]
        == "consumed"
    )
    assert (
        conn.execute("SELECT count(*) FROM locks WHERE lock_key='board1'").fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM evidence WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        == 3
    )


def test_board_rejects_arbitrary_shell_action(conn, config):
    cfg = executor_config(config, board=True)
    case_id, _ = create_case(
        conn, title="bug", case_type="bug", severity="P2", confidence=0.2
    )
    approved_board(conn, cfg, case_id)
    board = BoardExecutor(
        cfg, runner=lambda argv, cwd, timeout: ExecutionResult(argv, 0, "", "")
    )
    with pytest.raises(ExecutorError, match="allowlisted"):
        board.execute(
            conn,
            case_id=case_id,
            session_id="s1",
            action={"type": "shell", "command": "rm -rf /"},
        )


def test_board_executor_uses_configured_portable_tools(config):
    raw = copy.deepcopy(config.raw)
    raw["runtime"].update(
        {
            "serial_command": "/opt/board/bin/serial",
            "board_control_script": "/opt/board/board-ctrl.sh",
            "board_boot_script": "/opt/board/ram-boot.sh",
        }
    )
    cfg = Config(validate_config(raw), config.path)
    board = BoardExecutor(cfg)
    assert board._argv({"type": "reset"})[0] == [
        "bash",
        "/opt/board/board-ctrl.sh",
        "board1",
        "reset",
    ]
    assert board._argv({"type": "ram_boot"})[0] == [
        "bash",
        "/opt/board/ram-boot.sh",
        "--serial-alias",
        "board1",
    ]
    assert board._argv(
        {"type": "serial_wait", "regex": "BROM", "timeout": 5}
    )[0][:3] == ["/opt/board/bin/serial", "wait", "board1"]


def test_board_cleanup_can_retry_after_fresh_serial_timeout(conn, config):
    cfg = executor_config(config, board=True)
    case_id, _ = create_case(
        conn, title="cleanup retry", case_type="bug", severity="P2", confidence=0.2
    )
    approval_id = approved_board(conn, cfg, case_id)
    calls = []
    serial_attempts = 0

    def runner(argv, cwd, timeout):
        nonlocal serial_attempts
        calls.append(argv)
        if argv[:3] == ["serial", "wait", "board1"]:
            serial_attempts += 1
            if serial_attempts == 1:
                return ExecutionResult(argv, 2, "", "fresh marker timeout")
        return ExecutionResult(argv, 0, "fresh ROM: usb download handler", "")

    board = BoardExecutor(cfg, runner=runner)
    with pytest.raises(ExecutorError, match="exit 2"):
        board.close_session(conn, case_id=case_id, session_id="s1")
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()[0]
        == "approved"
    )
    assert conn.execute(
        "SELECT count(*) FROM locks WHERE lock_key='board1'"
    ).fetchone()[0] == 1

    board.close_session(conn, case_id=case_id, session_id="s1")
    assert serial_attempts == 2
    assert len(calls) == 4
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()[0]
        == "consumed"
    )
    states = [
        row[0]
        for row in conn.execute(
            "SELECT state FROM action_ledger WHERE action_type='board' ORDER BY created_at"
        )
    ]
    assert states.count("failed") == 1
    assert states.count("verified") == 3


def test_exact_wip_push_consumes_approval_and_verifies(conn, config):
    cfg = executor_config(config, wip_push=True)
    case_id, _ = create_case(
        conn, title="bug", case_type="bug", severity="P2", confidence=0.2
    )
    action = normalized_push_action(
        case_id=case_id,
        repo="u-boot",
        destination="refs/for/main%wip",
        commits=["a" * 40],
        command=["git", "push", "origin", "HEAD:refs/for/main%wip"],
    )
    action["binding"] = {
        "version": 1,
        "remote_url": "ssh://operator@gerrit.example.com:29418/uboot/uboot",
        "project": "uboot/uboot",
        "destination_branch": "main",
        "base_sha": "0" * 40,
        "tip_sha": "a" * 40,
    }
    conn.execute("UPDATE cases SET state='waiting_push' WHERE case_id=?", (case_id,))
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
        message_id="tg-push",
        decision_text="approve push",
        expected_digest=action_digest,
    )
    calls = []

    def runner(argv, cwd, timeout):
        calls.append(argv)
        if "--show-toplevel" in argv[-1]:
            return ExecutionResult(
                argv, 0, cfg.raw["repositories"]["u-boot"]["path"], ""
            )
        if "remote get-url" in argv[-1]:
            return ExecutionResult(argv, 0, action["binding"]["remote_url"], "")
        if "ls-remote" in argv[-1]:
            return ExecutionResult(argv, 0, "0" * 40 + "\trefs/heads/main\n", "")
        if "rev-list" in argv[-1]:
            return ExecutionResult(argv, 0, "a" * 40 + " " + "0" * 40, "")
        if "rev-parse" in argv[-1]:
            return ExecutionResult(argv, 0, "a" * 40 + "\n", "")
        return ExecutionResult(argv, 0, "remote: change 123", "")

    verified = execute_wip_push(
        conn,
        cfg,
        case_id=case_id,
        action=action,
        runner=runner,
        verifier=lambda action, result: {
            "change": "123",
            "revision": "a" * 40,
            "patch_set": 1,
            "wip": True,
        },
    )
    assert verified["wip"] is True
    assert calls[0][:2] == ["ssh", "buildhost"]
    assert "--show-toplevel" in calls[0][2]
    assert "push -o wip" in calls[-1][2]
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()[0]
        == "consumed"
    )


def test_wip_validator_refuses_force_or_ready_push():
    with pytest.raises(ExecutorError):
        validate_wip_action(
            {
                "mode": "WIP",
                "destination": "refs/for/main",
                "command": ["git", "push", "origin", "HEAD:refs/for/main"],
            }
        )
    with pytest.raises(ExecutorError):
        validate_wip_action(
            {
                "mode": "WIP",
                "destination": "refs/for/main%wip",
                "command": [
                    "git",
                    "push",
                    "--force",
                    "origin",
                    "HEAD:refs/for/main%wip",
                ],
            }
        )


def test_wip_validator_accepts_only_case_scoped_worktree():
    base = {
        "case_id": "K3-20260901-0001",
        "mode": "WIP",
        "repo": "u-boot",
        "destination": "refs/for/main%wip",
        "commits": ["a" * 40],
        "command": ["git", "push", "origin", "HEAD:refs/for/main%wip"],
        "binding": {
            "version": 1,
            "remote_url": "ssh://operator@gerrit.example.com:29418/uboot/uboot",
            "project": "uboot/uboot",
            "destination_branch": "main",
            "base_sha": "0" * 40,
            "tip_sha": "a" * 40,
        },
    }
    validate_wip_action(
        {
            **base,
            "worktree": "/data/home2/operator/WorkSpace/k3-ai-worktrees/K3-20260901-0001/u-boot",
        }
    )
    with pytest.raises(ExecutorError, match="exact Case root"):
        validate_wip_action(
            {**base, "worktree": "/data/home2/operator/WorkSpace/k3/uboot-2022.10"}
        )


def test_wip_validator_binds_exact_destination_and_full_commits():
    with pytest.raises(ExecutorError, match="refspec"):
        validate_wip_action(
            {
                "mode": "WIP",
                "destination": "refs/for/main%wip",
                "commits": ["a" * 40],
                "command": ["git", "push", "origin", "HEAD:refs/for/other%wip"],
            }
        )
    with pytest.raises(ExecutorError, match="40-character"):
        validate_wip_action(
            {
                "mode": "WIP",
                "destination": "refs/for/main%wip",
                "commits": ["abc123"],
                "command": ["git", "push", "origin", "HEAD:refs/for/main%wip"],
            }
        )


def test_production_gerrit_verifier_requires_exact_project_branch_revision_and_wip(
    config,
):
    cfg = executor_config(config, wip_push=True)
    action = normalized_push_action(
        case_id="K3-20260901-0001",
        repo="u-boot",
        destination="refs/for/main%wip",
        commits=["b" * 40],
        command=["git", "push", "origin", "HEAD:refs/for/main%wip"],
    )
    action["binding"] = {
        "version": 1,
        "remote_url": "ssh://operator@gerrit.example.com:29418/uboot/uboot",
        "project": "uboot/uboot",
        "destination_branch": "main",
        "base_sha": "0" * 40,
        "tip_sha": "b" * 40,
    }
    calls = []

    def runner(argv, cwd, timeout):
        calls.append(argv)
        if argv[:3] == ["ssh", "buildhost", "git"]:
            return ExecutionResult(
                argv, 0, "ssh://operator@gerrit.example.com:29418/uboot/uboot\n", ""
            )
        payload = {
            "number": 123,
            "project": "uboot/uboot",
            "branch": "main",
            "wip": True,
            "currentPatchSet": {"revision": "b" * 40, "number": 7},
        }
        stats = {"type": "stats", "rowCount": 1}
        output = "\n".join(__import__("json").dumps(item) for item in (payload, stats))
        return ExecutionResult(argv, 0, output, "")

    result = verify_gerrit_wip(
        cfg, action, ExecutionResult([], 0, "", ""), runner=runner
    )
    assert result["wip"] is True
    assert result["change"] == "123"
    assert result["patch_set"] == 7
    assert "gerrit query" in calls[0][2]
    assert "commit:" + "b" * 40 in calls[0][2]
