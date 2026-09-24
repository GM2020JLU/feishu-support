import os

import pytest
from test_executors import executor_config
from test_semantic_budget import policy

from k3_support import coding_budget
from k3_support.executors import (
    ExecutionResult,
    ExecutorError,
    create_codex_job,
    run_codex_job,
)
from k3_support.store import create_case, transition_case


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_identity_rejects_indirect_and_special_files(tmp_path, kind):
    from k3_support.model_budget import BudgetError

    target = tmp_path / "source.toml"
    target.write_text('model_provider="fixture"\n')
    target.chmod(0o600)
    path = tmp_path / "config.toml"
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.mkdir(mode=0o700)
    with pytest.raises((OSError, BudgetError)):
        coding_budget.identity(path)


def test_identity_rejects_change_during_read(tmp_path, monkeypatch):
    from k3_support.model_budget import BudgetError

    path = tmp_path / "config.toml"
    path.write_text('model_provider="fixture"\n')
    path.chmod(0o600)
    original = os.fstat
    calls = 0

    def changed(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text('model_provider="different-provider"\n')
        return original(fd)

    monkeypatch.setattr(coding_budget.os, "fstat", changed)
    with pytest.raises(BudgetError, match="changed"):
        coding_budget.identity(path)


def setup(conn, config):
    cfg = executor_config(config, codex=True)
    case, _ = create_case(
        conn, title="bug", case_type="bug", severity="P2", confidence=0.2
    )
    transition_case(
        conn,
        case_id=case,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="test",
        expected_version=1,
    )
    brief = "# UNTRUSTED INPUT\nmessage\n# FORBIDDEN ACTIONS\nNo push\n# ACCEPTANCE TESTS\nBuild"
    job, _ = create_codex_job(conn, cfg, case_id=case, brief=brief, repo="u-boot")
    return cfg, job


def test_actual_executor_precharges_and_does_not_restart_unknown_session(
    conn, config, monkeypatch
):
    cfg, job = setup(conn, config)
    policy(conn)
    monkeypatch.setattr(
        coding_budget,
        "identity",
        lambda path: {"provider": "fixture", "config_digest": "fixture"},
    )
    calls = []

    def runner(argv, cwd, timeout):
        row = conn.execute("SELECT * FROM model_budget_attempts").fetchone()
        assert row["state"] == "dispatched" and row["charged"] == 100
        assert row["case_id"] is not None and row["model"] == "gpt-5.6-sol"
        calls.append(1)
        raise TimeoutError("PRIVATE TRANSPORT DATA")

    with pytest.raises(ExecutorError, match="uncertain"):
        run_codex_job(conn, cfg, job_id=job, runner=runner)
    with pytest.raises(ExecutorError):
        run_codex_job(conn, cfg, job_id=job, runner=runner)
    assert calls == [1]
    row = conn.execute("SELECT * FROM model_budget_attempts").fetchone()
    assert row["state"] == "unknown" and row["charged"] == 100
    assert "PRIVATE" not in str(dict(row))


def test_config_identity_failure_never_starts_coding(conn, config, monkeypatch):
    cfg, job = setup(conn, config)
    policy(conn)

    def missing(path):
        raise OSError("private")

    monkeypatch.setattr(coding_budget, "identity", missing)
    with pytest.raises(ExecutorError, match="not started"):
        run_codex_job(
            conn,
            cfg,
            job_id=job,
            runner=lambda *args: pytest.fail("started without identity"),
        )
    assert conn.execute("SELECT count(*) FROM model_budget_attempts").fetchone()[0] == 0


def test_successful_coding_keeps_unknown_cost(conn, config, monkeypatch):
    cfg, job = setup(conn, config)
    policy(conn)
    monkeypatch.setattr(
        coding_budget,
        "identity",
        lambda path: {"provider": "fixture", "config_digest": "fixture"},
    )
    run_codex_job(
        conn,
        cfg,
        job_id=job,
        runner=lambda argv, *args: ExecutionResult(argv, 0, "done", ""),
    )
    row = conn.execute("SELECT state,charged FROM model_budget_attempts").fetchone()
    assert row["state"] == "unknown" and row["charged"] == 100


def test_identity_parses_only_config_descriptor_and_rejects_profile(tmp_path):
    from k3_support.model_budget import BudgetError

    path=tmp_path/"config.toml"
    path.write_text('model_provider = "fixture"\n[model_providers.fixture]\nexperimental_bearer_token="PRIVATE"\n')
    path.chmod(0o600)
    result=coding_budget.identity(path)
    assert result["provider"] == "fixture" and "PRIVATE" not in str(result)
    path.write_text('profile="selected"\nmodel_provider="fixture"\n')
    with pytest.raises(BudgetError,match="profile"):
        coding_budget.identity(path)
    path.write_text('model_provider="fixture"\n')
    path.chmod(0o666)
    with pytest.raises(BudgetError,match="trusted"):
        coding_budget.identity(path)
