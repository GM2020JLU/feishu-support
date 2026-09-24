"""Codex command adapter; deployment must provide an isolated workspace and policy."""

import json
from pathlib import Path

from .broker_execution_contract import ExecutionContract
from .broker_process import run_process


def executor(*, executable, workdir, environment, contract=None, remote_python=None):
    if not Path(executable).is_absolute() or not Path(workdir).is_absolute():
        raise ValueError("absolute executable and isolated workspace required")
    if contract is not None and (type(contract) is not ExecutionContract or contract.agent != "codex"
                                 or contract.wire_api != "responses"):
        raise ValueError("validated Codex execution contract required")
    model = contract.model if contract else "gpt-5.6-sol"
    reasoning = contract.reasoning if contract else "medium"
    command = [str(executable), "exec", "-m", model, "-c",
               "model_reasoning_effort=" + json.dumps(reasoning), "-c", 'approval_policy="never"',
               "-s", "workspace-write", "--skip-git-repo-check", "--ephemeral",
               "-C", str(workdir), "-"]
    env = dict(environment)
    if remote_python is not None:
        if not Path(remote_python).is_absolute():
            raise ValueError("absolute worker interpreter required")
        settings = {"mcp_servers.k3_remote.command": remote_python,
                    "mcp_servers.k3_remote.args": ["-I", "-m", "k3_support.broker_remote_mcp"],
                    "mcp_servers.k3_remote.env_vars": ["K3_SUPPORT_BROKER_TASK", "K3_SUPPORT_BROKER_SOCKET", "K3_SUPPORT_BROKER_CONTROL_UID"],
                    "mcp_servers.k3_remote.required": True,
                    "mcp_servers.k3_remote.enabled": True,
                    "mcp_servers.k3_remote.enabled_tools": ["verification_list", "remote_submit", "remote_read", "board_submit", "board_read"],
                    "mcp_servers.k3_remote.default_tools_approval_mode": "auto",
                    "mcp_servers.k3_remote.tool_timeout_sec": 10,
                    "shell_environment_policy.exclude": ["K3_SUPPORT_BROKER_*"]}
        # Only the fixed broker tools are preapproved at the native-client layer.
        # The broker still verifies the task grant, repository scope and board approval.
        for tool in settings["mcp_servers.k3_remote.enabled_tools"]:
            settings[f"mcp_servers.k3_remote.tools.{tool}.approval_mode"] = "approve"
        command[-1:-1] = [part for key, value in settings.items() for part in ("-c", key + "=" + json.dumps(value))]
    if contract is not None:
        if type(contract) is not ExecutionContract:
            raise ValueError("validated execution contract required")
        if contract.provider == "openai":
            # Current Codex reserves built-in provider IDs. Bind its endpoint using
            # the supported override instead of redefining the provider/auth flow.
            overrides = {"model_provider": "openai", "openai_base_url": contract.base_url}
            env["OPENAI_BASE_URL"] = contract.base_url
        else:
            overrides = {"model_provider": contract.provider,
                         f"model_providers.{contract.provider}.name": contract.provider,
                         f"model_providers.{contract.provider}.base_url": contract.base_url,
                         f"model_providers.{contract.provider}.wire_api": contract.wire_api}
        command[-1:-1] = [part for key, value in overrides.items()
                         for part in ("-c", key + "=" + json.dumps(value))]

    def execute(inputs, heartbeat):
        if inputs.get("model") != model or inputs.get("reasoning") != reasoning:
            raise ValueError("task model does not match configured Codex adapter")
        if "execution" in inputs and (contract is None or inputs["execution"] != contract.selection()):
            raise ValueError("task execution does not match configured Codex adapter")
        brief = inputs.get("brief")
        if not isinstance(brief, str) or not brief:
            raise ValueError("task brief required")
        if remote_python is not None:
            from .broker_verification_instructions import task_guidance
            brief += task_guidance(inputs)
            brief += ("\n\nRemote execution tool (do not access control databases or SSH credentials):\n"
                      "Use the k3_remote MCP remote_submit tool with request_id (canonical UUID), mode=inspect and command.\n"
                      "For scoped writable work use mode=work and repo=<assigned repository>.\n"
                      "Record the request UUID before sending. A missing response is unknown delivery, not permission to create another request.\n"
                      "Read using MCP remote_read with a new query request_id, remote_request_id=<original submission UUID>, offset=0.\n"
                      "Poll queued/running reads no more frequently than every 2 seconds; follow next_offset for output pages.\n"
                      "On unknown or cancelled state, stop and report uncertainty. Exit zero does not prove a repair or remote cleanup.\n"
                      "Task authority is supplied privately to the tool bridge. Never extract or include it in reports. Do not fall back to direct SSH or shell socket calls.\n")
            if inputs.get("board_session_id"):
                brief += ("\nAssigned board session (not approval): " + json.dumps(inputs["board_session_id"]) + "\n"
                          "Use MCP board_submit with request_id, this session_id and an allowlisted action; the broker checks occupancy approval.\n"
                          "Use board_read with board_request_id and offset to collect results, at least 2 seconds apart.\n"
                          "Never access USB, serial credentials, control databases or legacy board wrappers directly.\n"
                          "Stop on unknown or rejected actions and report the original request ID. Do not claim board cleanup from exit code alone.\n")
        # Full prompt travels through stdin, never argv or environment.
        return run_process(argv=command, cwd=str(workdir), env=env, stdin=brief.encode(),
                           heartbeat=heartbeat, timeout=7200, heartbeat_interval=10)

    return execute
