"""OpenCode ACP worker using a private config and broker-scoped tools."""

import json
import re
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from .broker_execution_contract import ExecutionContract
from .coding_acp import ACPError, Connection, ReportText


def _config(contract, credential_home):
    return {"$schema": "https://opencode.ai/config.json", "autoupdate": False,
            "share": "disabled", "snapshot": False, "enabled_providers": ["k3-broker"],
            "model": "k3-broker/" + contract.model, "small_model": "k3-broker/" + contract.model,
            "default_agent": "build", "permission": {"*": "deny", "k3_remote_*": "allow"},
            "compaction": {"auto": False, "prune": False}, "lsp": False, "formatter": False,
            "agent": {"title": {"disable": True}, "summary": {"disable": True}},
            "provider": {"k3-broker": {"npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": contract.base_url,
                            "apiKey": "{file:" + str(Path(credential_home) / "api-key") + "}"},
                "models": {contract.model: {"name": contract.model, "reasoning": True,
                    "options": {"reasoningEffort": contract.reasoning},
                    "variants": {contract.reasoning: {"reasoningEffort": contract.reasoning}}}}}}}


def _version(executable, environment):
    result = subprocess.run([str(executable), "--version"], env=environment,
                            capture_output=True, text=True, timeout=10, check=True)
    match = re.fullmatch(r"(?:opencode v)?([12])\.\d+\.\d+\s*", result.stdout)
    if match is None:
        raise ValueError("unsupported OpenCode version")
    return int(match[1])


def _config_v2(contract, mcp):
    return {"$schema": "https://opencode.ai/config.json", "update": "disable",
            "share": "disabled", "snapshots": False, "default_agent": "build",
            "model": "k3-broker/" + contract.model,
            "permissions": [{"action": "*", "resource": "*", "effect": "deny"},
                            {"action": "k3_remote_*", "resource": "*", "effect": "allow"}],
            "experimental": {"policies": [
                {"action": "provider.use", "resource": "*", "effect": "deny"},
                {"action": "provider.use", "resource": "k3-broker", "effect": "allow"}]},
            "compaction": {"auto": False}, "formatter": False,
            "mcp": {"servers": {"k3_remote": {
                "type": "local", "codemode": False,
                "command": [mcp["command"], *mcp["args"]],
                "environment": {item["name"]: item["value"] for item in mcp["env"]},
            }}},
            "providers": {"k3-broker": {"name": "K3 Broker",
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": contract.base_url,
                             "apiKey": "{env:K3_BROKER_API_KEY}"},
                "models": {contract.model: {"name": contract.model,
                    "modelID": contract.model,
                    "variants": [{"id": contract.reasoning,
                                  "settings": {"reasoningEffort": contract.reasoning}}]}}}}}


def executor(*, executable, workdir, environment, contract, remote_python):
    if (type(contract) is not ExecutionContract or contract.agent != "opencode"
            or contract.wire_api != "chat_completions"
            or contract.reasoning not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}):
        raise ValueError("validated OpenCode chat-completions contract required")
    env = dict(environment)
    credential_home = env.get("K3_SUPPORT_AGENT_HOME", "")
    if any(not Path(v).is_absolute() for v in (executable, workdir, remote_python, credential_home)):
        raise ValueError("absolute deployment paths required")
    if any((Path('/etc/opencode') / name).exists() for name in ('opencode.json', 'opencode.jsonc')):
        raise ValueError("managed OpenCode configuration requires deployment reconciliation")
    private_names = ("K3_SUPPORT_BROKER_TASK", "K3_SUPPORT_BROKER_SOCKET", "K3_SUPPORT_BROKER_CONTROL_UID")
    if any(not isinstance(env.get(key), str) or not env[key] for key in private_names):
        raise ValueError("private broker task environment required")
    mcp = {"name": "k3_remote", "command": str(remote_python),
           "args": ["-I", "-m", "k3_support.broker_remote_mcp"],
           "env": [{"name": key, "value": env[key]} for key in private_names]}

    def execute(inputs, heartbeat):
        if (inputs.get("execution") != contract.selection()
                or inputs.get("model") != contract.model or inputs.get("reasoning") != contract.reasoning):
            raise ValueError("task does not match configured OpenCode adapter")
        brief = inputs.get("brief")
        if not isinstance(brief, str) or not brief.strip():
            raise ValueError("task brief required")
        from .broker_verification_instructions import task_guidance
        brief += task_guidance(inputs)
        collected = ReportText()

        with TemporaryDirectory(prefix=".opencode-", dir=workdir) as private_home:
            version = _version(executable, env)
            if version == 2:
                # V2 scopes custom providers to a project Location. A disposable
                # private Git root makes the ACP session load only this policy.
                subprocess.run(["/usr/bin/git", "init", "-q", private_home],
                               check=True, capture_output=True, timeout=10)
                api_key = (Path(credential_home) / "api-key").read_text().strip()
                if not api_key:
                    raise ValueError("OpenCode credential unavailable")
                adapter_config = _config_v2(contract, mcp)
            else:
                api_key = None
                adapter_config = _config(contract, credential_home)
            child_env = {**env, "HOME": private_home, "OPENCODE_CONFIG_CONTENT": json.dumps(adapter_config),
                         **{key: str(Path(private_home) / key.lower()) for key in
                            ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")}}
            if version == 2:
                child_env["K3_BROKER_API_KEY"] = api_key
            for name in ("PROJECT_CONFIG", "DEFAULT_PLUGINS", "EXTERNAL_SKILLS", "CLAUDE_CODE",
                         "MODELS_FETCH", "AUTOUPDATE", "LSP_DOWNLOAD", "SHARE"):
                child_env["OPENCODE_DISABLE_" + name] = "true"
            for name in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_AUTH_CONTENT",
                         "OPENCODE_TEST_MANAGED_CONFIG_DIR"):
                child_env.pop(name, None)
            with Connection(argv=[str(executable), "acp"], cwd=private_home if version == 2 else str(workdir), env=child_env,
                            heartbeat=heartbeat, on_update=collected.update) as client:
                client.initialize()
                # V2 registers configured MCP tools after session/new returns.
                # Exposing them as native tools avoids Code Mode's separate
                # execute permission and keeps the existing narrow allowlist.
                client.new_session(mcp_servers=[] if version == 2 else [mcp])
                client.select_option("model", "k3-broker/" + contract.model)
                client.select_option("effort", contract.reasoning)
                if version == 2:
                    # Controlled V2.0.12 probes found the MCP catalog absent
                    # immediately after selection and present after settling.
                    # Keep this bounded; no broker operation is attempted here.
                    time.sleep(3)
                client.prompt(brief + "\n\nUse only k3_remote MCP tools for repository operations. "
                    "Record a canonical UUID request_id before remote_submit; use mode inspect or work "
                    "and an assigned repo for work. Poll remote_read with a fresh query request_id and "
                    "the original remote_request_id, no more often than every 2 seconds; follow next_offset. "
                    "Never repeat an operation on unknown delivery. Report original IDs and uncertainty. "
                    "Board operations still require broker-verified session approval.\n")
                client.request("session/close", {"sessionId": client.session_id})
        report = collected.text()
        if not report.strip():
            raise ACPError("OpenCode did not return a report")
        return report

    return execute
