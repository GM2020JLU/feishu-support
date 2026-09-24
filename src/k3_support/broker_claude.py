"""Claude Code adapter using only the control broker's scoped MCP operations.

The worker's private CLAUDE_CONFIG_DIR supplies OAuth or an api-key file. Deployment
isolation is still required; command flags are not an operating-system sandbox.
"""

import json
import os
import stat
from pathlib import Path

from .broker_execution_contract import ExecutionContract
from .broker_process import run_process


def _api_key(directory):
    """Optional native API-key file; malformed credentials never fall back."""
    if not Path(directory).is_absolute() or ".." in Path(directory).parts:
        raise ValueError("absolute native credential directory required")
    root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(root)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("private worker credential directory required")
        try:
            fd = os.open("api-key", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root)
        except FileNotFoundError:
            return None  # Native OAuth remains available when no API key is configured.
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or before.st_mode & 0o077 or before.st_nlink != 1):
                raise ValueError("private regular API key required")
            raw = stream.read(16385)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("credential changed during read")
        key = raw.strip()
        if not key or len(raw) > 16384 or any(byte < 33 or byte > 126 for byte in key):
            raise ValueError("invalid API key file")
        return key.decode("ascii")
    finally:
        os.close(root)


def executor(*, executable, workdir, environment, contract, remote_python):
    if (type(contract) is not ExecutionContract or contract.agent != "claude"
            or contract.wire_api != "messages"):
        raise ValueError("validated Claude execution contract required")
    if any(not Path(value).is_absolute() for value in (executable, workdir, remote_python)):
        raise ValueError("absolute deployment paths required")
    env = dict(environment)
    env["ANTHROPIC_BASE_URL"] = contract.base_url
    if env.get("CLAUDE_CONFIG_DIR"):
        key = _api_key(env["CLAUDE_CONFIG_DIR"])
        if key is not None:
            env["ANTHROPIC_API_KEY"] = key
    private_names = ("K3_SUPPORT_BROKER_TASK", "K3_SUPPORT_BROKER_SOCKET", "K3_SUPPORT_BROKER_CONTROL_UID")
    if any(not isinstance(env.get(key), str) or not env[key] for key in private_names):
        raise ValueError("private broker task environment required")
    mcp = {"mcpServers": {"k3_remote": {"type": "stdio", "command": str(remote_python),
            "args": ["-I", "-m", "k3_support.broker_remote_mcp"],
            "env": {key: "${" + key + "}" for key in private_names}}}}
    command = [str(executable), "--print", "--output-format", "json", "--model", contract.model,
               "--effort", contract.reasoning, "--permission-mode", "dontAsk", "--restricted",
               "--tools", "", "--setting-sources", "", "--disable-slash-commands",
               "--strict-mcp-config", "--mcp-config", json.dumps(mcp, separators=(",", ":")),
               "--no-session-persistence"]

    def execute(inputs, heartbeat):
        if (inputs.get("execution") != contract.selection()
                or inputs.get("model") != contract.model or inputs.get("reasoning") != contract.reasoning):
            raise ValueError("task does not match configured Claude adapter")
        brief = inputs.get("brief")
        if not isinstance(brief, str) or not brief.strip():
            raise ValueError("task brief required")
        from .broker_verification_instructions import task_guidance
        brief += task_guidance(inputs)
        tools = ["verification_list", "remote_submit", "remote_read"]
        if inputs.get("board_session_id"):
            tools += ["board_submit", "board_read"]
        argv = command + ["--allowedTools", ",".join("mcp__k3_remote__" + name for name in tools)]
        prompt = brief + (
            "\n\nUse only the k3_remote MCP tools for repository work. "
            "remote_submit accepts a canonical UUID request_id, mode=inspect or mode=work, "
            "an assigned repo for work, and command. Record each request_id before sending. "
            "Read remote_read with a fresh query request_id and the original remote_request_id. "
            "Poll no more often than every 2 seconds and follow next_offset. "
            "Unknown delivery or execution is not permission to repeat an operation with a new ID. "
            "Stop on unknown or cancelled operations. Report uncertainty and the original ID. "
            "Do not access control data, credentials, SSH, USB or serial directly. "
            "A zero exit code is not proof of a repair, tests or cleanup.\n"
        )
        if inputs.get("board_session_id"):
            prompt += ("Assigned board session (not approval): " + json.dumps(inputs["board_session_id"])
                       + ". board_submit/board_read still require broker-verified occupancy approval.\n")
        raw = run_process(argv=argv, cwd=str(workdir), env=env, stdin=prompt.encode(),
                          heartbeat=heartbeat, timeout=7200, heartbeat_interval=10)
        try:
            result = json.loads(raw)
        except (ValueError, RecursionError):
            raise ValueError("invalid Claude completion envelope") from None
        if (not isinstance(result, dict) or result.get("type") != "result"
                or result.get("subtype") != "success" or result.get("is_error") is not False
                or result.get("permission_denials", []) != []
                or not isinstance(result.get("result"), str) or not result["result"].strip()):
            raise ValueError("Claude did not return a successful report")
        return result["result"]

    return execute
