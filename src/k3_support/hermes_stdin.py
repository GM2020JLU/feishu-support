"""Version-pinned stdin bridge. This is not a same-UID security sandbox."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import inspect
import io
import json
import os
import stat
import sys
import time
from pathlib import Path

MAX_INPUT = 2 * 1024 * 1024
RECEIPT_SOURCES = frozenset({"agent/conversation_loop.py", "agent/usage_pricing.py",
                             "hermes_cli/lifecycle.py", "hermes_cli/plugins.py",
                             "model_tools.py", "toolsets.py", "agent/agent_init.py",
                             "agent/tool_executor.py", "agent/agent_runtime_helpers.py"})


class BridgeError(ValueError):
    pass


FAILURE_CODES = frozenset({'request_identity_mismatch', 'tools_exposed',
                          'receipt_fields_missing', 'response_identity_mismatch',
                          'incomplete_response', 'unexpected_api_count',
                          'model_requested_tools', 'multiple_api_requests'})
PHASES = frozenset({'import_started', 'import_finished', 'cli_started',
                    'request_validated', 'response_received', 'result_parsed'})


class BridgeAbort(SystemExit):
    def __init__(self, reason):
        super().__init__(2)
        self.reason = reason if reason in FAILURE_CODES else 'runtime_exit'


def failure_code(error):
    if isinstance(error, BridgeAbort):
        return error.reason
    if isinstance(error, json.JSONDecodeError):
        if not error.doc.strip():
            return 'empty_json_output'
        if error.msg == 'Extra data':
            return 'extra_json_output'
        return 'invalid_json_output'
    if isinstance(error, BridgeError):
        return 'bridge_validation_failed'
    if isinstance(error, SystemExit):
        return 'runtime_exit'
    return 'runtime_failed'


def manifest(path):
    if not path or not Path(path).is_absolute():
        raise BridgeError("absolute bridge manifest is required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise BridgeError("bridge manifest must be owner-only")
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise BridgeError("bridge manifest too large")
    value = json.loads(raw)
    return validate_manifest(value)


def validate_manifest(value):
    fields = {
        "schema_version",
        "python",
        "root",
        "model",
        "provider",
        "cli_sha256",
        "run_agent_sha256",
    }
    if not isinstance(value, dict):
        raise BridgeError("invalid bridge manifest schema")
    allowed = (fields,)
    if value.get("schema_version") == 2:
        allowed = (fields | {"receipt_sources"}, fields | {"receipt_sources", "runtime_provider", "endpoint_sha256"})
    if (
        set(value) not in allowed
        or type(value["schema_version"]) is not int
        or value["schema_version"] not in {1, 2}
    ):
        raise BridgeError("invalid bridge manifest schema")
    if "runtime_provider" in value and (
        not isinstance(value["runtime_provider"], str) or not 1 <= len(value["runtime_provider"]) <= 256
        or not isinstance(value["endpoint_sha256"], str) or len(value["endpoint_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in value["endpoint_sha256"])
    ):
        raise BridgeError("invalid runtime provider binding")
    for key in fields - {"schema_version"}:
        if not isinstance(value[key], str) or not value[key] or "\x00" in value[key]:
            raise BridgeError("invalid bridge manifest field")
    for key in ("python", "root"):
        if not Path(value[key]).is_absolute():
            raise BridgeError("bridge paths must be absolute")
    if not Path(value["python"]).is_file() or not os.access(value["python"], os.X_OK):
        raise BridgeError("Hermes interpreter is unavailable")
    for name, key in (("cli.py", "cli_sha256"), ("run_agent.py", "run_agent_sha256")):
        source = Path(value["root"]) / name
        if source.is_symlink() or not source.is_file() or source.stat().st_mode & 0o022:
            raise BridgeError("unsafe Hermes source file")
        if hashlib.sha256(source.read_bytes()).hexdigest() != value[key]:
            raise BridgeError("Hermes source version changed")
    if value["schema_version"] == 2:
        sources = value["receipt_sources"]
        if not isinstance(sources, dict) or set(sources) != RECEIPT_SOURCES:
            raise BridgeError("incomplete receipt source manifest")
        for name, expected in sources.items():
            source = Path(value["root"]) / name
            for part in (source, *source.parents):
                if part.is_symlink() or part.stat().st_mode & 0o022:
                    raise BridgeError("unsafe receipt source path")
                if part == Path(value["root"]):
                    break
            if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise BridgeError("receipt source version changed")
    return value


def manifest_template(*, root, python, model, provider, runtime_provider=None, endpoint_sha256=None):
    if any(not isinstance(value, str) or not value for value in (root, python, model, provider)):
        raise BridgeError("explicit root, interpreter, model and provider required")
    if not Path(root).is_absolute() or not Path(python).is_absolute():
        raise BridgeError("absolute Hermes paths required")
    def fingerprint(name):
        source = Path(root) / name
        if not source.is_file() or source.is_symlink():
            raise BridgeError("missing or unsafe Hermes source")
        return hashlib.sha256(source.read_bytes()).hexdigest()
    value = {"schema_version": 2, "root": root, "python": python, "model": model,
             "provider": provider, "cli_sha256": fingerprint("cli.py"),
             "run_agent_sha256": fingerprint("run_agent.py"),
             "receipt_sources": {name: fingerprint(name) for name in sorted(RECEIPT_SOURCES)}}
    if runtime_provider is not None or endpoint_sha256 is not None:
        value.update(runtime_provider=runtime_provider, endpoint_sha256=endpoint_sha256)
    return validate_manifest(value)


class BoundedText(io.StringIO):
    def __init__(self):
        super().__init__()
        self.size = 0

    def write(self, value):
        self.size += len(value.encode("utf-8"))
        if self.size > MAX_INPUT:
            raise BridgeError("adapter output exceeded limit")
        return super().write(value)


class InferenceReceipt:
    """One-call observation, not proof of weights or settled provider billing."""

    def __init__(self, model, provider, *, runtime_provider=None, endpoint_sha256=None):
        self.model, self.provider = model, provider
        self.runtime_provider = runtime_provider or provider
        self.endpoint_sha256 = endpoint_sha256
        self.calls = 0
        self.request_attempts = 0
        self.receipt = None
        self.rejection_reason = 'receipt_fields_missing'

    def endpoint_matches(self, event):
        if self.endpoint_sha256 is None:
            return True
        endpoint = event.get("base_url")
        return (isinstance(endpoint, str) and bool(endpoint)
                and hashlib.sha256(endpoint.rstrip("/").encode()).hexdigest() == self.endpoint_sha256)

    def observe(self, **event):
        self.calls += 1
        if self.calls != 1:
            self.receipt = None
            self.rejection_reason = 'multiple_api_requests'
            return
        names = ("api_request_id", "session_id", "model", "provider", "response_model")
        if any(not isinstance(event.get(key), str) or not 0 < len(event[key]) <= 256 for key in names):
            return
        if (event["model"] != self.model or event["provider"] != self.runtime_provider
                or not self.endpoint_matches(event)
                or event["response_model"] != self.model):
            self.rejection_reason = 'response_identity_mismatch'
            return
        if event.get('finish_reason') != 'stop':
            self.rejection_reason = 'incomplete_response'
            return
        if type(event.get('api_call_count')) is not int or event['api_call_count'] != 1:
            self.rejection_reason = 'unexpected_api_count'
            return
        if type(event.get('assistant_tool_call_count')) is not int or event['assistant_tool_call_count'] != 0:
            self.rejection_reason = 'model_requested_tools'
            return
        # Never retain full response, prompt, endpoint, assistant text or raw usage.
        self.receipt = {key: event[key] for key in names}
        self.receipt["finish_reason"] = "stop"
        if self.endpoint_sha256 is not None:
            self.receipt["requested_provider"] = self.provider
            self.receipt["endpoint_sha256"] = self.endpoint_sha256
        usage = event.get("usage")
        fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        self.receipt["usage"] = (
            {key: usage[key] for key in fields}
            if isinstance(usage, dict) and all(type(usage.get(key)) is int and 0 <= usage[key] <= 10**12 for key in fields)
            else None
        )
        self.receipt["billing_verified"] = False

    def finish(self):
        if self.calls != 1 or self.receipt is None:
            raise BridgeError("missing or inconsistent inference receipt")
        return dict(self.receipt)


@contextlib.contextmanager
def without_model_tools(cli_module, agent_module, tools_module):
    """Suppress definitions and central dispatch in this single-use process.

    Not an OS sandbox: imports, plugins and alternate inline dispatch paths need
    separate controls. The protocol-2 pre-dispatch receipt gate remains required.
    """
    def definitions(*args, **kwargs):
        return []

    def deny(*args, **kwargs):
        raise SystemExit(2)

    for module in (agent_module, tools_module):
        if any(not callable(getattr(module, name, None)) for name in ("get_tool_definitions", "handle_function_call")):
            raise BridgeError("unsupported Hermes tool interface")
    original = []
    try:
        for module in (cli_module, agent_module, tools_module):
            for name, replacement in (("get_tool_definitions", definitions), ("handle_function_call", deny)):
                if hasattr(module, name):
                    original.append((module, name, getattr(module, name)))
                    setattr(module, name, replacement)
        yield
    finally:
        for module, name, value in reversed(original):
            setattr(module, name, value)


@contextlib.contextmanager
def observe_inference(lifecycle, collector, *, progress=lambda _: None):
    """Temporary interception in the isolated, single-query bridge process."""
    original_has, original_invoke = lifecycle.has_hook, lifecycle.invoke_hook

    def has_hook(name):
        return name in {"pre_api_request", "post_api_request"} or original_has(name)

    def invoke_hook(name, **event):
        if name == 'pre_api_request':
            collector.request_attempts += 1
            if collector.request_attempts != 1:
                collector.receipt = None
                collector.rejection_reason = 'multiple_api_requests'
                raise BridgeAbort('multiple_api_requests')
        if (name == "pre_api_request"
                and (event.get("provider") != collector.runtime_provider or event.get("model") != collector.model
                     or not collector.endpoint_matches(event))):
            raise BridgeAbort('request_identity_mismatch')
        if name == "pre_api_request" and (type(event.get("tool_count")) is not int or event["tool_count"] != 0):
            # Check the final agent tool surface, including schemas appended
            # after get_tool_definitions by context/memory providers.
            raise BridgeAbort('tools_exposed')
        if name == "post_api_request":
            progress('response_received')
            collector.observe(**event)
            if collector.receipt is None:
                # Hermes emits this event before tool dispatch, but suppresses
                # ordinary hook Exceptions. SystemExit aborts the isolated CLI
                # before a rejected response can invoke skill_manage or any tool.
                # main converts it to a generic failure without echoing content.
                raise BridgeAbort(collector.rejection_reason)
        if name == 'pre_api_request':
            progress('request_validated')
        return original_invoke(name, **event)

    lifecycle.has_hook, lifecycle.invoke_hook = has_hook, invoke_hook
    try:
        yield
    finally:
        lifecycle.has_hook, lifecycle.invoke_hook = original_has, original_invoke


def _invoke_cli(entry, kwargs):
    try:
        entry(**kwargs)
    except SystemExit as error:
        if error.code is not None and not (type(error.code) is int and error.code == 0):
            raise


@contextlib.contextmanager
def without_reasoning_display(module):
    """Quiet CLI still enables configured reasoning display; keep it off stdout.

    This changes rendering only, not the model's reasoning effort or response.
    Restore the class callback even if the isolated request aborts.
    """
    cli_class = getattr(module, 'HermesCLI', None)
    if cli_class is None:  # Minimal synthetic entrypoints have no UI class.
        yield
        return
    original = cli_class._current_reasoning_callback
    cli_class._current_reasoning_callback = lambda self: None
    try:
        yield
    finally:
        cli_class._current_reasoning_callback = original


def execute(value, request, *, entry=None, lifecycle=None, progress=lambda _: None):
    required = {"protocol", "prompt", "reasoning"}
    if not isinstance(request, dict) or set(request) not in (
        required, required | {"expected_manifest_digest"}
    ):
        raise BridgeError("invalid stdin request")
    if "expected_manifest_digest" in request:
        actual = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
        if request["expected_manifest_digest"] != actual:
            raise BridgeError("bridge identity changed before invocation")
    if type(request["protocol"]) is not int or request["protocol"] not in {1, 2} or request["reasoning"] not in {"low", "medium"}:
        raise BridgeError("unsupported stdin protocol")
    if request["protocol"] == 2 and "expected_manifest_digest" not in request:
        raise BridgeError("receipt mode requires manifest binding")
    if request["protocol"] == 2 and value.get("schema_version") != 2:
        raise BridgeError("receipt mode requires version 2 source manifest")
    if not isinstance(request["prompt"], str) or not request["prompt"]:
        raise BridgeError("missing prompt")
    output, errors = BoundedText(), BoundedText()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), contextlib.ExitStack() as guards:
        if entry is None:
            progress('import_started')
            sys.path.insert(0, value["root"])
            module = importlib.import_module("cli")
            if (
                Path(module.__file__).resolve()
                != (Path(value["root"]) / "cli.py").resolve()
            ):
                raise BridgeError("unexpected Hermes entrypoint")
            entry = module.main
            guards.enter_context(without_reasoning_display(module))
            guards.enter_context(without_model_tools(module, importlib.import_module("run_agent"),
                                                     importlib.import_module("model_tools")))
            progress('import_finished')
        kwargs = {
            "query": request["prompt"],
            "reasoning": request["reasoning"],
            "model": value["model"],
            "provider": value["provider"],
            "skills": "k3-support-orchestrator",
            "toolsets": "skills",
            "quiet": True,
            "max_turns": 1,
            "ignore_rules": True,
        }
        inspect.signature(entry).bind(**kwargs)
        if request["protocol"] == 2:
            if lifecycle is None:
                lifecycle = importlib.import_module("hermes_cli.lifecycle")
            collector = InferenceReceipt(value["model"], value["provider"],
                                         runtime_provider=value.get("runtime_provider"),
                                         endpoint_sha256=value.get("endpoint_sha256"))
            with observe_inference(lifecycle, collector, progress=progress):
                progress('cli_started')
                _invoke_cli(entry, kwargs)
            receipt = collector.finish()
        else:
            _invoke_cli(entry, kwargs)
    result = json.loads(output.getvalue().strip())
    if not isinstance(result, dict):
        raise BridgeError("model result must be one JSON object")
    progress('result_parsed')
    if request["protocol"] == 2:
        return {"protocol": 2, "result": result, "receipt": receipt,
                "manifest_digest": request["expected_manifest_digest"],
                "request_digest": hashlib.sha256(json.dumps(request, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--support-json-stdin", action="store_true")
    mode.add_argument("--manifest-template", action="store_true",
                      help="Print a checked version-2 manifest; no model call or config write")
    parser.add_argument("--hermes-root")
    parser.add_argument("--runtime-provider")
    parser.add_argument("--endpoint-sha256")
    parser.add_argument("--hermes-python")
    parser.add_argument("--model")
    parser.add_argument("--provider")
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate local manifest without model calls",
    )
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument('--diagnostics', action='store_true', help='Emit fixed phase names and elapsed milliseconds only, never model content.')
    args = parser.parse_args()
    try:
        if args.manifest_template:
            value = manifest_template(root=args.hermes_root, python=args.hermes_python,
                                      model=args.model, provider=args.provider,
                                      runtime_provider=args.runtime_provider, endpoint_sha256=args.endpoint_sha256)
            print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        value = manifest(os.environ.get("K3_SUPPORT_HERMES_BRIDGE_CONFIG"))
        if args.check:
            print(
                json.dumps(
                    {
                        "protocol": 1,
                        "manifest_valid": True,
                        "manifest_schema": value["schema_version"],
                        "supported_protocols": [1, 2] if value["schema_version"] == 2 else [1],
                        "checked_source_count": 2 + (len(RECEIPT_SOURCES) if value["schema_version"] == 2 else 0),
                        "model": value["model"],
                        "provider": value["provider"],
                        "inference_verified": False,
                    }
                )
            )
            return 0
        if not args.child:
            # Re-exec into the declared Hermes environment, preserving stdin.
            # Neither the OS argv nor environment ever carries the prompt.
            os.execv(
                value["python"],
                [
                    value["python"],
                    "-I",
                    str(Path(__file__).resolve()),
                    "--support-json-stdin",
                    "--child",
                    *(['--diagnostics'] if args.diagnostics else []),
                ],
            )
        if Path(sys.executable).resolve() != Path(value["python"]).resolve():
            raise BridgeError("unexpected Hermes interpreter")
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise BridgeError("stdin request too large")
        diagnostic_stream = sys.stderr
        started = time.monotonic()

        def progress(phase):
            if args.diagnostics and phase in PHASES:
                print(json.dumps({'bridge_phase': phase, 'elapsed_ms': round((time.monotonic()-started)*1000)}),
                      file=diagnostic_stream, flush=True)

        result = execute(value, json.loads(raw), progress=progress)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Exception, SystemExit) as error:  # noqa: BLE001 - never echo input-bearing adapter errors
        print(
            json.dumps({'error': 'bridge_failure', 'failure_code': failure_code(error)}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
