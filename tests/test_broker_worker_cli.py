import json
import os
import signal
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from k3_support import broker_worker_cli as worker


def settings(tmp_path):
    home = tmp_path / "worker"
    home.mkdir(mode=0o700)
    codex = home / ".codex"
    codex.mkdir(mode=0o700)
    root = home / "tasks"
    root.mkdir(mode=0o700)
    return {"socket_path": str(tmp_path / "broker.sock"), "control_uid": os.geteuid()+1,
            "claim_request_id": str(uuid4()), "executable": sys.executable,
            "home": str(home), "codex_home": str(codex), "workspace_root": str(root)}


def argv(values):
    names = {"socket_path": "socket", "executable": "codex-executable"}
    return [arg for key, value in values.items() for arg in ("--"+names.get(key, key.replace("_", "-")), str(value))]


def test_entrypoint_uses_explicit_environment_and_never_control_config(tmp_path, monkeypatch, capsys):
    values = settings(tmp_path)
    monkeypatch.setenv("CONTROL_SECRET", "do-not-inherit")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.invalid:1234")
    seen = []
    monkeypatch.setattr(worker, "executor", lambda **kw: seen.append(kw) or (lambda *_: "unused"))
    def one(**kwargs):
        assert kwargs["claim_request_id"] == values["claim_request_id"]
        kwargs["prepare_executor"]({"model": "gpt-5.6-sol", "reasoning": "medium"}, task={"fixture": "task"})
        assert kwargs["transport"]({"method": "synthetic"}) == {"result": "fixture"}
        return {"state": "idle"}
    monkeypatch.setattr(worker, "run_one", one)
    monkeypatch.setattr(worker, "request_at", lambda path, value, **kw: {"result": "fixture"})
    assert worker.main(argv(values)) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "idle"}
    call = seen[0]
    assert call["environment"] == {"HOME": values["home"], "CODEX_HOME": values["codex_home"],
                                   "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                                   "K3_SUPPORT_BROKER_SOCKET": values["socket_path"],
                                   "K3_SUPPORT_BROKER_CONTROL_UID": str(values["control_uid"]),
                                   "K3_SUPPORT_BROKER_TASK": '{"fixture":"task"}'}
    assert call["workdir"] == values["workspace_root"]+"/"+values["claim_request_id"]


@pytest.mark.parametrize("problem", ["same_uid", "relative", "public", "symlink", "claim_symlink", "credentials_in_worktree"])
def test_invalid_deployment_never_claims(tmp_path, monkeypatch, problem):
    from pathlib import Path

    values = settings(tmp_path)
    if problem == "same_uid":
        values["control_uid"] = os.geteuid()
    elif problem == "relative":
        values["home"] = "relative"
    elif problem == "public":
        Path(values["workspace_root"]).chmod(0o755)
    elif problem == "symlink":
        link = tmp_path / "link"
        link.symlink_to(values["workspace_root"], target_is_directory=True)
        values["workspace_root"] = str(link)
    elif problem == "claim_symlink":
        (Path(values["workspace_root"]) / values["claim_request_id"]).symlink_to(values["home"], target_is_directory=True)
    else:
        values["workspace_root"] = values["home"]
    if problem == "claim_symlink":
        def one(**kwargs):
            kwargs["prepare_executor"]({"model": "gpt-5.6-sol", "reasoning": "medium"}, task={})
            pytest.fail("must not start execution")
        monkeypatch.setattr(worker, "run_one", one)
    else:
        monkeypatch.setattr(worker, "run_one", lambda **_: pytest.fail("must not claim"))
    assert worker.main(argv(values)) == 1


def test_failure_is_redacted_and_not_retried(tmp_path, monkeypatch, capsys):
    values = settings(tmp_path)
    calls = []
    def fail(**_):
        calls.append(True)
        raise ValueError("private-credential-or-report")
    monkeypatch.setattr(worker, "run_one", fail)
    assert worker.main(argv(values)) == 1
    output = capsys.readouterr()
    assert "private-credential-or-report" not in output.err + output.out
    assert calls == [True]


def test_control_username_and_signal_handlers_are_restored(tmp_path, monkeypatch, capsys):
    values = settings(tmp_path)
    expected_uid = values.pop("control_uid")
    values["control_user"] = "control-fixture"
    monkeypatch.setattr(worker.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=expected_uid))
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    def stopped(**kwargs):
        assert kwargs["control_uid"] == expected_uid
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        raise worker.WorkerStopped(kwargs["stop_requested"]())
    monkeypatch.setattr(worker, "run", stopped)
    assert worker.main(argv(values)) == 143
    assert {sig: signal.getsignal(sig) for sig in previous} == previous
    assert "reconciliation" in capsys.readouterr().err


def test_idle_claim_does_not_create_a_workspace(tmp_path, monkeypatch, capsys):
    from pathlib import Path

    values = settings(tmp_path)
    def idle(path, request, **kwargs):
        assert request["method"] == "claim"
        return {"version": 1, "request_id": request["request_id"], "ok": True, "result": {"task": None}}
    monkeypatch.setattr(worker, "request_at", idle)
    monkeypatch.setattr(worker, "executor", lambda **_: pytest.fail("idle must not prepare execution"))
    assert worker.main(argv(values)) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "idle"}
    assert list(Path(values["workspace_root"]).iterdir()) == []


@pytest.mark.parametrize("agent,wire_api,home_key", [("claude", "messages", "CLAUDE_CONFIG_DIR"),
                                                     ("dsh", "chat_completions", "DSH_HOME"),
                                                     ("opencode", "chat_completions", "K3_SUPPORT_AGENT_HOME"),
                                                     ("hermes", "chat_completions", "K3_SUPPORT_AGENT_HOME")])
def test_bound_contract_dispatches_adapter_with_private_native_home(tmp_path, monkeypatch, capsys, agent, wire_api, home_key):
    from k3_support.broker_execution_contract import ExecutionContract

    values = settings(tmp_path)
    directory = tmp_path / "contract"
    directory.mkdir(mode=0o700)
    values["contract_directory"] = str(directory)
    policy = ExecutionContract("fixture", "https://provider.example", "bound-model", "high",
                               wire_api, "a" * 64, agent=agent)
    monkeypatch.setattr(worker, "load_at", lambda *_args, **_kwargs: policy)
    monkeypatch.setattr(worker, "executor", lambda **_: pytest.fail("must not use Codex"))
    seen = []
    monkeypatch.setattr(f"k3_support.broker_{agent}.executor", lambda **kwargs: seen.append(kwargs) or (lambda *_: "unused"))
    def one(**kwargs):
        assert kwargs["executor_agent"] == agent
        assert kwargs["contract_fingerprint"] == policy.fingerprint
        kwargs["prepare_executor"]({"model": policy.model, "reasoning": policy.reasoning,
                                     "execution": policy.selection()}, task={"fixture": "private-task"})
        return {"state": "idle"}
    monkeypatch.setattr(worker, "run_one", one)
    arguments = [part.replace("--codex-executable", "--agent-executable").replace("--codex-home", "--agent-home")
                 for part in argv(values)]
    assert worker.main(arguments) == 0
    assert seen[0]["contract"] is policy
    assert seen[0]["environment"][home_key] == values["codex_home"]
    assert "CODEX_HOME" not in seen[0]["environment"]
    assert "private-task" not in capsys.readouterr().out


@pytest.mark.parametrize("agent", ["codex", "claude", "dsh", "opencode", "hermes"])
def test_catalog_selection_prepares_only_selected_native_adapter(tmp_path, monkeypatch, agent):
    from k3_support.broker_catalog import Catalog, Profile
    from k3_support.broker_execution_contract import ExecutionContract

    values = settings(tmp_path)
    directory = tmp_path / 'contracts'
    directory.mkdir(mode=0o700)
    policy = ExecutionContract('fixture', 'https://example.com', 'selected-model', 'high',
                               'responses' if agent == 'codex' else 'messages' if agent == 'claude' else 'chat_completions',
                               'a' * 64, agent=agent)
    profile = Profile(agent, policy, values['executable'], values['codex_home'], 'http://127.0.0.1:12345')
    monkeypatch.setenv('HTTPS_PROXY', 'http://ambient.invalid:1234')
    # Ownership/manifest validation is exercised with real files in catalog tests.
    monkeypatch.setattr(Catalog, 'profiles', lambda _: (profile,))
    values.update(executable=None, codex_home=None, contract_directory=str(directory), execution_catalog=True)
    seen = []
    target = 'k3_support.broker_worker_cli.executor' if agent == 'codex' else f'k3_support.broker_{agent}.executor'
    monkeypatch.setattr(target, lambda **kwargs: seen.append(kwargs) or (lambda *_: 'unused'))
    def one(**kwargs):
        inputs = {'model': policy.model, 'reasoning': policy.reasoning, 'execution': policy.selection()}
        assert kwargs['select_contract'](inputs) == policy
        kwargs['prepare_executor'](inputs, task={'synthetic': 'binding'})
        return {'state': 'synthetic_prepared'}
    monkeypatch.setattr(worker, 'run_one', one)
    assert worker.run(**values)['state'] == 'synthetic_prepared'
    assert len(seen) == 1 and seen[0]['contract'] == policy
    assert seen[0]['executable'] == profile.executable
    assert seen[0]['environment']['HTTPS_PROXY'] == profile.proxy_url
    assert seen[0]['environment']['NO_PROXY'] == 'localhost,127.0.0.1,::1'


def test_catalog_mode_rejects_mixed_cli_paths(tmp_path):
    values = settings(tmp_path)
    with pytest.raises(SystemExit):
        worker.main(argv(values) + ['--execution-catalog', '--contract-directory', str(tmp_path)])
