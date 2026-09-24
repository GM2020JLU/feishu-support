"""DeepSeek Harness ACP adapter with an explicit broker-only plugin composition."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .broker_execution_contract import ExecutionContract
from .coding_acp import ACPError, Connection, ReportText


def _profile(contract, credential_home):
    # An empty bundle list avoids inheriting native shell/file/subagent plugins,
    # user settings, telemetry, and plugin changes in the ordinary base bundle.
    plugins = ["timer", "llm", "session", "session-title", "session-projection",
               "system-prompt", "tools", "agent", "agent-loop", "llm-retry", "jobs-local"]
    rows = [{"id": name, "name": "@deepseek-ai/" + (
        "cordis-plugin-timer" if name == "timer" else "dsh-" + name)} for name in plugins]
    next(row for row in rows if row["id"] == "session-title")["config"] = {
        "fallbackMaxWords": 5, "fallbackMaxBytes": 40, "maxTitleBytes": 80}
    rows += [
        {"id": "credentials", "name": "@deepseek-ai/dsh-credentials-local",
         "config": {"path": str(Path(credential_home) / ".credentials.yaml"), "watch": False}},
        {"id": "llm-deepseek", "name": "@deepseek-ai/dsh-llm-deepseek",
         "config": {"baseURL": contract.base_url, "apiKeyEnv": "DEEPSEEK_API_KEY",
                    "models": [{"id": contract.model}], "reasoningEffort": contract.reasoning}},
        {"id": "sessions", "name": "@deepseek-ai/dsh-session-persistence-jsonl",
         "config": {"compression": "none"}},
        {"id": "acp-app-startup", "name": "@deepseek-ai/dsh-acp-app"},
        {"id": "acp", "name": "@deepseek-ai/dsh-acp", "inject": ["acpAppStartup", "agentLoop"],
         "config": {"provider": "deepseek-official", "model": contract.model}},
    ]
    return [{"insert": rows}]


def executor(*, executable, workdir, environment, contract, remote_python):
    if (type(contract) is not ExecutionContract or contract.agent != "dsh"
            or contract.wire_api != "chat_completions"
            or contract.reasoning not in {"off", "low", "high", "max"}):
        raise ValueError("validated DSH chat-completions contract required")
    env = dict(environment)
    credential_home = env.get("DSH_HOME", "")
    if any(not Path(v).is_absolute() for v in (executable, workdir, remote_python, credential_home)):
        raise ValueError("absolute deployment paths required")
    private_names = ("K3_SUPPORT_BROKER_TASK", "K3_SUPPORT_BROKER_SOCKET", "K3_SUPPORT_BROKER_CONTROL_UID")
    if any(not isinstance(env.get(key), str) or not env[key] for key in private_names):
        raise ValueError("private broker task environment required")
    env["DEEPSEEK_BASE_URL"] = contract.base_url
    env["DSH_TELEMETRY_DISABLED"] = "1"
    mcp = {"name": "k3_remote", "command": str(remote_python),
           "args": ["-I", "-m", "k3_support.broker_remote_mcp"],
           "env": [{"name": key, "value": env[key]} for key in private_names]}

    def execute(inputs, heartbeat):
        if (inputs.get("execution") != contract.selection()
                or inputs.get("model") != contract.model or inputs.get("reasoning") != contract.reasoning):
            raise ValueError("task does not match configured DSH adapter")
        brief = inputs.get("brief")
        if not isinstance(brief, str) or not brief.strip():
            raise ValueError("task brief required")
        from .broker_verification_instructions import task_guidance
        brief += task_guidance(inputs)
        collected = ReportText()

        with TemporaryDirectory(prefix=".dsh-", dir=workdir) as private_home:
            profile = Path(private_home) / "profiles" / "k3-broker"
            profile.mkdir(parents=True, mode=0o700)
            (profile / "package.json").write_text(json.dumps({"private": True, "type": "module",
                "dsh": {"profile": {"bundles": [], "patchReload": "startup"}}}))
            config = _profile(contract, credential_home)
            for row in config[0]["insert"]:
                if row["id"] == "sessions":
                    row["config"]["root"] = str(Path(private_home) / "sessions")
            (profile / "cordis.patch.yml").write_text(json.dumps(config))
            child_env = {**env, "DSH_HOME": private_home}
            with Connection(argv=[str(executable), "--profile", "k3-broker"], cwd=str(workdir),
                            env=child_env, heartbeat=heartbeat, on_update=collected.update) as client:
                client.initialize()
                client.new_session(mcp_servers=[mcp])
                client.select_option("model", json.dumps(["deepseek-official", contract.model], separators=(",", ":")))
                client.select_option("reasoning_effort", contract.reasoning)
                client.prompt(brief + "\n\nUse only k3_remote MCP tools for repository operations. "
                    "Record a canonical UUID request_id before remote_submit; use mode inspect or work "
                    "and an assigned repo for work. Poll remote_read with a fresh query request_id and "
                    "the original remote_request_id, no more often than every 2 seconds; follow next_offset. "
                    "Never repeat an operation on unknown delivery. Report original IDs and uncertainty. "
                    "Board operations still require broker-verified session approval.\n")
                client.request("session/close", {"sessionId": client.session_id})
        report = collected.text()
        if not report.strip():
            raise ACPError("DSH did not return a report")
        return report

    return execute
