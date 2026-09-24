import tomllib

import pytest
from test_broker_execution_contract import contract, read_contract

from k3_support.broker_codex import executor


def test_validated_contract_drives_actual_codex_overrides(tmp_path, monkeypatch):
    policy = read_contract(tmp_path, contract())
    seen = []
    monkeypatch.setattr("k3_support.broker_codex.run_process", lambda **kw: seen.append(kw) or "report")
    callback = executor(executable="/synthetic/codex", workdir=tmp_path, environment={}, contract=policy)
    callback({"model": "gpt-5.6-sol", "reasoning": "medium", "brief": "synthetic prompt"}, lambda: None)
    argv = seen[0]["argv"]
    overrides = tomllib.loads("\n".join(argv[i+1] for i, part in enumerate(argv) if part == "-c"))
    assert overrides["model_provider"] == policy.provider
    assert overrides["model_providers"][policy.provider] == {
        "name": policy.provider, "base_url": policy.base_url, "wire_api": "responses"}
    assert overrides["model_reasoning_effort"] == "medium"
    assert overrides["approval_policy"] == "never"
    assert argv[-1] == "-" and seen[0]["stdin"] == b"synthetic prompt"


def test_version_two_task_drives_bound_model_and_reasoning(tmp_path, monkeypatch):
    policy = read_contract(tmp_path, {**contract(), "version": 2, "agent": "codex",
                                     "model": "bound-model", "reasoning": "high"})
    seen = []
    monkeypatch.setattr("k3_support.broker_codex.run_process", lambda **kw: seen.append(kw) or "report")
    callback = executor(executable="/synthetic/codex", workdir=tmp_path, environment={}, contract=policy)
    callback({"model": policy.model, "reasoning": policy.reasoning, "brief": "synthetic prompt",
              "execution": policy.selection()}, lambda: None)
    argv = seen[0]["argv"]
    assert argv[argv.index("-m")+1] == "bound-model"
    overrides = tomllib.loads("\n".join(argv[i+1] for i, part in enumerate(argv) if part == "-c"))
    assert overrides["model_reasoning_effort"] == "high"


def test_codex_adapter_preserves_policy_and_uses_stdin(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("k3_support.broker_codex.run_process", lambda **kw: seen.append(kw) or "report")
    callback = executor(executable="/synthetic/codex", workdir=tmp_path, environment={"HOME": str(tmp_path)})
    brief = "private colleague question"
    assert callback({"model": "gpt-5.6-sol", "reasoning": "medium", "brief": brief}, lambda: None) == "report"
    call = seen[0]
    assert call["stdin"] == brief.encode()
    assert brief not in str(call["argv"]) and brief not in str(call["env"])
    assert call["argv"][-1] == "-"
    assert "workspace-write" in call["argv"]
    assert "--ignore-rules" not in call["argv"]
    assert "danger-full-access" not in call["argv"]
    assert call["timeout"] == 7200


@pytest.mark.parametrize("field,value", [("model", "other"), ("reasoning", "high"), ("brief", "")])
def test_task_cannot_override_adapter_policy(tmp_path, monkeypatch, field, value):
    def forbidden(**kwargs):
        raise AssertionError("must not launch")
    monkeypatch.setattr("k3_support.broker_codex.run_process", forbidden)
    inputs = {"model": "gpt-5.6-sol", "reasoning": "medium", "brief": "synthetic"}
    inputs[field] = value
    callback = executor(executable="/synthetic/codex", workdir=tmp_path, environment={})
    with pytest.raises(ValueError):
        callback(inputs, lambda: None)


def test_builtin_openai_binds_endpoint_without_redefining_reserved_provider(tmp_path, monkeypatch):
    policy = read_contract(tmp_path, {**contract(), "provider": "openai"})
    seen = []
    monkeypatch.setattr("k3_support.broker_codex.run_process", lambda **kw: seen.append(kw) or "report")
    callback = executor(executable="/synthetic/codex", workdir=tmp_path,
                        environment={"OPENAI_BASE_URL": "https://unbound.example"}, contract=policy)
    callback({"model": policy.model, "reasoning": policy.reasoning, "brief": "fixture"}, lambda: None)
    argv = seen[0]["argv"]
    overrides = tomllib.loads("\n".join(argv[i+1] for i, part in enumerate(argv) if part == "-c"))
    assert overrides["model_provider"] == "openai"
    assert overrides["openai_base_url"] == policy.base_url
    assert "model_providers" not in overrides
    assert seen[0]["env"]["OPENAI_BASE_URL"] == policy.base_url


def test_native_preapproval_is_limited_to_fixed_broker_tools(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("k3_support.broker_codex.run_process", lambda **kw: seen.append(kw) or "report")
    callback = executor(executable="/synthetic/codex", workdir=tmp_path, environment={},
                        remote_python="/synthetic/python")
    callback({"model": "gpt-5.6-sol", "reasoning": "medium", "brief": "fixture"}, lambda: None)
    argv = seen[0]["argv"]
    settings = tomllib.loads("\n".join(argv[i+1] for i, part in enumerate(argv) if part == "-c"))
    assert settings["approval_policy"] == "never"
    assert set(settings["mcp_servers"]) == {"k3_remote"}
    broker = settings["mcp_servers"]["k3_remote"]
    assert set(broker["enabled_tools"]) == {"verification_list", "remote_submit", "remote_read", "board_submit", "board_read"}
    assert broker["tools"] == {name: {"approval_mode": "approve"} for name in broker["enabled_tools"]}
    assert broker["default_tools_approval_mode"] == "auto"
    assert "workspace-write" in argv
