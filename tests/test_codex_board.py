from __future__ import annotations

import copy
from pathlib import Path

import pytest

from k3_support.approvals import (
    decide_approval,
    expiry_after,
    normalized_board_action,
    request_approval,
)
from k3_support.codex_board import CodexBoardError, execute_codex_board_action
from k3_support.config import Config, validate_config
from k3_support.executors import BoardExecutor, ExecutionResult, create_codex_job
from k3_support.store import create_case, transition_case


def active_config(config) -> Config:
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"].update({"codex": True, "board": True})
    raw["repositories"] = {
        "u-boot": {
            "path": "/data/home2/operator/WorkSpace/k3/uboot-2022.10",
            "remote": "origin",
            "base_branch": "main",
        }
    }
    return Config(validate_config(raw), config.path)


def test_board_wrapper_does_not_initialize_missing_control_database(config):
    assert not config.database_path.exists()
    with pytest.raises(CodexBoardError, match="existing private"):
        execute_codex_board_action(config, case_id="missing", session_id="missing", action={"type": "list"})
    assert not config.database_path.exists()


def test_board_wrapper_never_migrates_older_control_database(conn, config):
    latest = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
    conn.execute("DELETE FROM schema_migrations WHERE version=?", (latest,))
    before = list(conn.iterdump())
    with pytest.raises(CodexBoardError, match="migration"):
        execute_codex_board_action(config, case_id="missing", session_id="missing", action={"type": "reset"})
    assert list(conn.iterdump()) == before


def test_codex_board_wrapper_requires_exact_job_capability_and_session(
    conn, config, monkeypatch
):
    cfg = active_config(config)
    case_id, _ = create_case(
        conn,
        title="board bug",
        case_type="bug",
        severity="P2",
        confidence=0.5,
        requester_id="ou_colleague",
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="triage",
        expected_version=1,
    )
    transition_case(
        conn,
        case_id=case_id,
        after="investigating",
        actor_type="system",
        actor_id=None,
        reason="investigate",
        expected_version=2,
    )
    session_id = case_id + "-board-1"
    action = normalized_board_action(case_id, session_id, 30)
    approval_id, action_digest, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id=session_id,
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
        message_id="tg-board",
        decision_text="approve board",
        expected_digest=action_digest,
    )
    transition_case(
        conn,
        case_id=case_id,
        after="waiting_board",
        actor_type="system",
        actor_id=None,
        reason="wait",
        expected_version=3,
    )
    transition_case(
        conn,
        case_id=case_id,
        after="board_testing",
        actor_type="system",
        actor_id=None,
        reason="approved",
        expected_version=4,
    )
    brief = (
        "# UNTRUSTED INPUT\nx\n# FORBIDDEN ACTIONS\nNo push\n# ACCEPTANCE TESTS\nBoard"
    )
    job_id, _ = create_codex_job(
        conn,
        cfg,
        case_id=case_id,
        brief=brief,
        repo="u-boot",
        context_extra={"board_session_id": session_id, "phase": "board"},
    )
    row = conn.execute("SELECT workdir FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    capability = Path(row["workdir"], ".capability").read_text(encoding="utf-8").strip()
    conn.execute("UPDATE jobs SET state='running',lease_owner='board-worker',lease_expires_at=? WHERE job_id=?", (expiry_after(30), job_id))
    attempt = conn.execute("SELECT attempt_no FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
    monkeypatch.setenv("K3_SUPPORT_LEASE_OWNER", "board-worker")
    monkeypatch.setenv("K3_SUPPORT_EXECUTION_ROUND", str(attempt))
    monkeypatch.setenv("K3_SUPPORT_JOB_ID", job_id)
    monkeypatch.setenv("K3_SUPPORT_CASE_ID", case_id)
    monkeypatch.setenv("K3_SUPPORT_CAPABILITY", capability)
    calls = []
    executor = BoardExecutor(
        cfg,
        runner=lambda argv, cwd, timeout: (
            calls.append(argv) or ExecutionResult(argv, 0, "fresh U-Boot>", "")
        ),
    )
    result = execute_codex_board_action(
        cfg,
        case_id=case_id,
        session_id=session_id,
        action={"type": "serial_wait", "regex": "U-Boot>", "timeout": 15},
        executor=executor,
    )
    assert result.returncode == 0
    assert calls and "--after" in calls[0]
    assert (
        conn.execute(
            "SELECT count(*) FROM evidence WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        == 1
    )
    original_context = conn.execute("SELECT context_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
    original_calls = len(calls)
    for variable, value in (("K3_SUPPORT_LEASE_OWNER", "another-worker"), ("K3_SUPPORT_EXECUTION_ROUND", str(attempt+1))):
        with monkeypatch.context() as changed:
            changed.setenv(variable, value)
            with pytest.raises(CodexBoardError, match="lease or attempt"):
                execute_codex_board_action(cfg, case_id=case_id, session_id=session_id,
                                          action={"type": "reset"}, executor=executor)
    conn.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    with pytest.raises(CodexBoardError, match="lease expired"):
        execute_codex_board_action(cfg, case_id=case_id, session_id=session_id,
                                  action={"type": "reset"}, executor=executor)
    conn.execute("UPDATE jobs SET lease_expires_at=?", (expiry_after(30),))
    from contextlib import contextmanager
    @contextmanager
    def changed_while_waiting(_):
        conn.execute("UPDATE jobs SET lease_owner='changed-under-lock'")
        yield
    with monkeypatch.context() as changed:
        changed.setattr("k3_support.board_operation_guard.hold", changed_while_waiting)
        with pytest.raises(CodexBoardError, match="lease or attempt"):
            execute_codex_board_action(cfg, case_id=case_id, session_id=session_id,
                                      action={"type": "reset"}, executor=executor)
    conn.execute("UPDATE jobs SET lease_owner='board-worker'")
    assert len(calls) == original_calls
    for malformed in ("[]", "null", '"text"', "1"):
        conn.execute("UPDATE jobs SET context_json=? WHERE job_id=?", (malformed, job_id))
        with pytest.raises(CodexBoardError, match="context is invalid"):
            execute_codex_board_action(cfg, case_id=case_id, session_id=session_id,
                                      action={"type": "reset"}, executor=executor)
    assert len(calls) == original_calls
    conn.execute("UPDATE jobs SET context_json=? WHERE job_id=?", (original_context, job_id))
    conn.execute("UPDATE cases SET lifecycle_round=lifecycle_round+1 WHERE case_id=?", (case_id,))
    call_count = len(calls)
    with pytest.raises(CodexBoardError, match="running Codex"):
        execute_codex_board_action(cfg, case_id=case_id, session_id=session_id,
                                  action={"type": "reset"}, executor=executor)
    assert len(calls) == call_count
    conn.execute("UPDATE cases SET lifecycle_round=lifecycle_round-1 WHERE case_id=?", (case_id,))
    monkeypatch.setenv("K3_SUPPORT_CAPABILITY", "forged")
    with pytest.raises(CodexBoardError, match="mismatch"):
        execute_codex_board_action(
            cfg,
            case_id=case_id,
            session_id=session_id,
            action={"type": "reset"},
            executor=executor,
        )
