"""Bounded stdio MCP bridge; authority comes from the worker, never tool input."""

import json
import sys

from jsonschema import Draft202012Validator

from .broker_remote_tool import invoke


def _schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


ID = {"type": "string", "maxLength": 36, "minLength": 36,
      "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
BOARD_ACTION = {"oneOf": [
    _schema({"type": {"enum": ["list", "reset", "enter_brom"]}}, ["type"]),
    _schema({"type": {"const": "ram_boot"}, "uboot_only": {"type": "boolean"},
             "timeout": {"type": "integer", "minimum": 30, "maximum": 900}}, ["type"]),
    _schema({"type": {"const": "serial_wait"}, "regex": {"type": "string", "minLength": 1, "maxLength": 512},
             "timeout": {"type": "integer", "minimum": 1, "maximum": 600}}, ["type", "regex", "timeout"]),
    _schema({"type": {"const": "serial_exec"}, "command": {"type": "array", "minItems": 1, "maxItems": 64,
              "items": {"type": "string", "minLength": 1, "maxLength": 1024}},
             "expect": {"type": "string", "minLength": 1, "maxLength": 512},
             "timeout": {"type": "integer", "minimum": 1, "maximum": 300}}, ["type", "command", "expect", "timeout"]),
]}
TOOLS = [
    {"name": "verification_list", "description": "Read control-prepared verification requests for this task. Start with after_id empty and follow next_cursor. Before verification work, fetch this list. Only submit an item with dispatchable=true: use its remote_request_id unchanged as remote_submit request_id and copy its exact remote fields. If already submitted or delivery is unknown, only remote_read that original ID. An empty list is not permission to invent a verified result. Listing grants no new authority.",
     "inputSchema": _schema({"request_id": ID, "after_id": {"type": "string", "maxLength": 256}}, ["request_id"]),
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "remote_submit", "description": "Queue one scoped command. For inspect, omit repo or use null; for work, supply the assigned repo. Inspect exposes source repositories only and hides Case workspaces. To read an existing checkout or its logs, use work with the assigned repo even for read-only commands. Missing paths in inspect do not prove workspace deletion. Mode selects filesystem visibility, not permission to exceed the task instructions. Wait for the current command to settle before submitting another. Preserve request_id on unknown delivery; never resubmit with a new ID.",
     "inputSchema": _schema({"request_id": ID, "mode": {"enum": ["inspect", "work"]},
                             "repo": {"type": ["string", "null"], "maxLength": 100,
                                      "pattern": "^[A-Za-z0-9_.-]{1,100}$",
                                      "description": "Omit or null for inspect; assigned repository name for work."},
                             "command": {"type": "string", "minLength": 1, "maxLength": 32768}},
                            ["request_id", "mode", "command"]),
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}},
    {"name": "remote_read", "description": "Read an existing operation. Poll at least 2 seconds apart; follow next_offset. Unknown is not permission to rerun.",
     "inputSchema": _schema({"request_id": ID, "remote_request_id": ID,
                             "offset": {"type": "integer", "minimum": 0, "maximum": 128000}},
                            ["request_id", "remote_request_id"]),
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "board_submit", "description": "Queue a board action in the exact approved session. A session ID is not approval. Preserve request_id; never retry unknown actions.",
     "inputSchema": _schema({"request_id": ID, "session_id": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,256}$"},
                             "action": BOARD_ACTION}, ["request_id", "session_id", "action"]),
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}},
    {"name": "board_read", "description": "Read this task's board operation. Poll at least 2 seconds apart. Command success does not establish BROM cleanup or repair.",
     "inputSchema": _schema({"request_id": ID, "board_request_id": ID,
                             "offset": {"type": "integer", "minimum": 0, "maximum": 128000}}, ["request_id", "board_request_id"]),
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
]


REMOTE_SUBMIT_MODE_RULES = {"allOf": [
    {"if": {"properties": {"mode": {"const": "inspect"}}, "required": ["mode"]},
     "then": {"properties": {"repo": {"type": "null"}}}},
    {"if": {"properties": {"mode": {"const": "work"}}, "required": ["mode"]},
     "then": {"required": ["repo"], "properties": {"repo": {"type": "string"}}}},
]}


def argument_error(tool, arguments):
    """Expose only trusted schema names, never values or unknown property names."""
    error = next(Draft202012Validator(tool['inputSchema']).iter_errors(arguments), None)
    # Claude omits tools with root-level allOf/if/then. Advertise the mode rule
    # in the description and enforce it here before any broker delivery.
    if error is None and tool['name'] == 'remote_submit':
        error = next(Draft202012Validator(REMOTE_SUBMIT_MODE_RULES).iter_errors(arguments), None)
    if error is None:
        return None
    field = next(iter(error.path), 'arguments')
    if field not in tool['inputSchema']['properties']:
        field = 'arguments'
    return f"Invalid arguments: {field} violates {error.validator}. Check the advertised tool schema."


class Bridge:
    def __init__(self, operation=invoke):
        self.operation = operation
        self.initialized = False
        self.ready = False

    def handle(self, value):
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise ValueError("invalid RPC")
        method, ident = value.get("method"), value.get("id")
        if "id" not in value:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        if type(ident) not in (str, int) or (isinstance(ident, str) and len(ident) > 256):
            raise ValueError("invalid RPC ID")
        response = {"jsonrpc": "2.0", "id": ident}
        if method == "initialize" and not self.initialized:
            params = value.get("params", {})
            version = params.get("protocolVersion") if isinstance(params, dict) else None
            if not isinstance(version, str):
                raise ValueError("invalid initialization")
            self.initialized = True
            response["result"] = {"protocolVersion": version if version in ("2024-11-05", "2025-03-26", "2025-06-18") else "2025-06-18",
                                  "capabilities": {"tools": {}}, "serverInfo": {"name": "k3-remote", "version": "0.1.0"}}
        elif method == "ping":
            response["result"] = {}
        elif not self.ready:
            response["error"] = {"code": -32600, "message": "Initialize the bridge first"}
        elif method == "tools/list":
            response["result"] = {"tools": TOOLS}
        elif method == "tools/call":
            params = value.get("params", {})
            tool = next((t for t in TOOLS if isinstance(params, dict) and t["name"] == params.get("name")), None)
            arguments = params.get("arguments", {}) if isinstance(params, dict) else None
            invalid = argument_error(tool, arguments) if tool else "Invalid tool. Use an advertised tool name."
            if invalid:
                response["error"] = {"code": -32602, "message": invalid}
            else:
                try:
                    operation = {"remote_submit": "submit", "remote_read": "read", "board_submit": "board_submit", "board_read": "board_read", "verification_list": "verification_list"}[tool["name"]]
                    result = self.operation(operation=operation, **arguments)
                    response["result"] = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=True)}], "isError": False}
                except (OSError, ValueError, KeyError, TypeError):
                    response["result"] = {"content": [{"type": "text", "text": "Broker request rejected or delivery unknown. Keep the original request ID; do not rerun."}], "isError": True}
        else:
            response["error"] = {"code": -32601, "message": "Method unavailable"}
        return response


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def serve(source, sink, bridge=None):
    bridge = bridge or Bridge()
    while True:
        raw = source.readline(262145)
        if not raw:
            return 0
        if len(raw) > 262144 or not raw.endswith(b"\n"):
            return 1  # Never interpret the remainder of an oversized frame.
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            response = bridge.handle(value)
        except (ValueError, RecursionError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid message"}}
        if response is not None:
            encoded = (json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n").encode()
            if len(encoded) > 262144:
                return 1
            sink.write(encoded)
            sink.flush()


def main():
    try:
        return serve(sys.stdin.buffer, sys.stdout.buffer)
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
