"""Bounded single-session ACP transport for deployment-selected coding agents.

No filesystem or terminal client capabilities are advertised. Agents still need
an independently isolated execution environment: ACP is a protocol, not a sandbox.
Permission requests default to cancellation; tool labels never grant authority.
"""

import json
import math
import os
import selectors
import signal
import subprocess
import time


class ACPError(ValueError):
    """A protocol failure whose message contains no remote payload."""



class ReportText:
    """Keep streamed text intact, separating messages across tool/thought activity."""

    def __init__(self):
        self.chunks = []
        self.boundary = False

    def update(self, value):
        kind = value.get("sessionUpdate")
        if kind in {"tool_call", "tool_call_update", "agent_thought_chunk"}:
            self.boundary = True
        if kind != "agent_message_chunk":
            return
        content = value.get("content", {})
        if not isinstance(content, dict) or content.get("type") != "text":
            return
        text = content.get("text")
        if not isinstance(text, str):
            raise ACPError("invalid ACP report fragment")
        if not text:
            return
        if self.boundary and self.chunks:
            self.chunks.append("\n\n")
        self.boundary = False
        self.chunks.append(text)

    def text(self):
        return "".join(self.chunks)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ACPError("duplicate ACP field")
        result[key] = value
    return result


class Connection:
    """One owned child, one outstanding request, bounded lifetime and buffers.

    Call within a with-block. heartbeat and on_update are trusted, bounded local
    callbacks; they must not block on user input. Failure stops the child group.
    A deployment cgroup is required to contain descendants that escape the group.
    """

    def __init__(self, *, argv, cwd, env, heartbeat, timeout=7200,
                 heartbeat_interval=10, output_limit=2097152, frame_limit=262144,
                 on_update=lambda _: None):
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in argv)
                or not os.path.isabs(argv[0]) or not os.path.isabs(cwd)
                or not isinstance(env, dict)):
            raise ValueError("explicit absolute ACP command, workspace and environment required")
        if (any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                for v in (timeout, heartbeat_interval)) or heartbeat_interval > timeout
                or type(output_limit) is not int or not 1 <= output_limit <= 16777216
                or type(frame_limit) is not int or not 1 <= frame_limit <= output_limit):
            raise ValueError("invalid ACP limits")
        self.argv, self.cwd, self.env = list(argv), cwd, dict(env)
        self.heartbeat, self.on_update = heartbeat, on_update
        self.timeout, self.interval = timeout, heartbeat_interval
        self.output_limit, self.frame_limit = output_limit, frame_limit
        self.process = None
        self.selector = None
        self.session_id = None
        self.config_options = []
        self.sequence = 0
        self.pending_id = None
        self.pending_method = None
        self.pre_session_updates = []
        self.response = None
        self.outgoing = bytearray()
        self.incoming = bytearray()
        self.output_bytes = 0
        self.failed = False
        self.permission_denied = False
        self.tool_failed = False

    def __enter__(self):
        if self.process is not None:
            raise ACPError("ACP connection cannot be reused")
        self.heartbeat()
        self.started = time.monotonic()
        self.next_heartbeat = self.started + self.interval
        self.process = subprocess.Popen(self.argv, cwd=self.cwd, env=self.env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True,
                                        close_fds=True)
        try:
            self.selector = selectors.DefaultSelector()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                os.set_blocking(stream.fileno(), False)
            self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
            self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        if self.selector is not None:
            self.selector.close()
        if self.process is not None:
            # Do not poll/reap before signalling: reserve the PID for group cleanup.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    stream.close()
                self.process.wait(timeout=5)
        self.closed = True

    def _queue(self, value):
        packet = json.dumps(value, ensure_ascii=False, allow_nan=False,
                            separators=(",", ":")).encode() + b"\n"
        if len(packet) > self.frame_limit or len(self.outgoing) + len(packet) > self.frame_limit:
            raise ACPError("ACP request limit exceeded")
        self.outgoing.extend(packet)
        try:
            self.selector.get_key(self.process.stdin)
        except KeyError:
            self.selector.register(self.process.stdin, selectors.EVENT_WRITE, "stdin")

    def _receive(self, frame):
        try:
            value = json.loads(frame, object_pairs_hook=_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ACPError("invalid ACP number")))
        except (ValueError, UnicodeError, RecursionError):
            raise ACPError("invalid ACP frame") from None
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise ACPError("invalid ACP envelope")
        if "method" in value:
            method, params = value["method"], value.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ACPError("invalid ACP method")
            if "id" in value:
                request_id = value["id"]
                if type(request_id) not in (int, str):
                    raise ACPError("invalid ACP request identity")
                if method == "session/request_permission":
                    if self.session_id is None or params.get("sessionId") != self.session_id:
                        raise ACPError("permission request for different session")
                    self.permission_denied = True
                    # No model-supplied command, path or option ID is authority.
                    self._queue({"jsonrpc": "2.0", "id": request_id,
                                 "result": {"outcome": {"outcome": "cancelled"}}})
                else:
                    self._queue({"jsonrpc": "2.0", "id": request_id,
                                 "error": {"code": -32601, "message": "Client method unavailable"}})
            elif method == "session/update":
                if self.session_id is None:
                    # OpenCode may announce capabilities while session/new is
                    # pending. Do not trust the provisional ID until the result
                    # names the same session; retain only a bounded batch.
                    if (self.pending_method != "session/new"
                            or not isinstance(params.get("sessionId"), str)
                            or not isinstance(params.get("update"), dict)
                            or len(self.pre_session_updates) >= 32):
                        raise ACPError("update for different session")
                    self.pre_session_updates.append(params)
                else:
                    self._accept_update(params)
            # Unknown notifications confer no authority and are ignored.
            return
        if (type(value.get("id")) is not int or value["id"] != self.pending_id
                or self.response is not None or ("result" in value) == ("error" in value)):
            raise ACPError("uncorrelated ACP response")
        if "error" in value:
            raise ACPError("ACP agent rejected request")
        if not isinstance(value["result"], dict):
            raise ACPError("invalid ACP result")
        self.response = value["result"]

    def _accept_update(self, params):
        if (params.get("sessionId") != self.session_id
                or not isinstance(params.get("update"), dict)):
            raise ACPError("update for different session")
        update = params["update"]
        if (update.get("sessionUpdate") in {"tool_call", "tool_call_update"}
                and update.get("status") == "failed"):
            self.tool_failed = True
        self.on_update(update)

    def _pump(self):
        now = time.monotonic()
        if now - self.started >= self.timeout:
            raise TimeoutError("ACP execution deadline exceeded")
        if now >= self.next_heartbeat:
            self.heartbeat()
            self.next_heartbeat = time.monotonic() + self.interval
        for key, _ in self.selector.select(min(.1, max(0, self.next_heartbeat - now),
                                               self.timeout - (now - self.started))):
            stream = key.fileobj
            if key.data == "stdin":
                try:
                    written = os.write(stream.fileno(), self.outgoing[:16384])
                except BrokenPipeError:
                    raise ACPError("ACP input closed") from None
                del self.outgoing[:written]
                if not self.outgoing:
                    self.selector.unregister(stream)
                continue
            chunk = os.read(stream.fileno(), 16384)
            if not chunk:
                self.selector.unregister(stream)
                if key.data == "stdout":
                    raise ACPError("ACP output closed before session settlement")
                continue
            self.output_bytes += len(chunk)
            if self.output_bytes > self.output_limit:
                raise ACPError("ACP output limit exceeded")
            if key.data == "stderr":
                continue  # Count and discard potentially sensitive diagnostics.
            self.incoming.extend(chunk)
            while b"\n" in self.incoming:
                length = self.incoming.index(b"\n")
                if length + 1 > self.frame_limit:
                    raise ACPError("ACP frame limit exceeded")
                frame = bytes(self.incoming[:length])
                del self.incoming[:length + 1]
                self._receive(frame)
            if len(self.incoming) >= self.frame_limit:
                raise ACPError("ACP frame limit exceeded")

    def request(self, method, params):
        if (self.process is None or getattr(self, "closed", False) or self.failed
                or self.pending_id is not None):
            raise ACPError("ACP connection is not ready")
        self.sequence += 1
        self.pending_id = self.sequence
        self.pending_method = method
        self.response = None
        try:
            self._queue({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
            while self.response is None or self.outgoing:
                self._pump()
            return self.response
        except BaseException:
            self.failed = True
            raise
        finally:
            self.pending_id = None
            self.pending_method = None

    def initialize(self):
        result = self.request("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                             "clientInfo": {"name": "k3-support", "version": "0.1.0"}})
        if type(result.get("protocolVersion")) is not int or result["protocolVersion"] != 1:
            raise ACPError("unsupported ACP protocol version")
        return result

    def new_session(self, *, mcp_servers=()):
        if self.session_id is not None:
            raise ACPError("only one fresh ACP session is allowed")
        result = self.request("session/new", {"cwd": self.cwd, "mcpServers": list(mcp_servers)})
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 256:
            raise ACPError("invalid ACP session identity")
        self.session_id = session_id
        self.config_options = result.get("configOptions", [])
        for params in self.pre_session_updates:
            self._accept_update(params)
        self.pre_session_updates.clear()
        return result

    def select_option(self, option_id, value):
        """Select an exact advertised opaque value; never silently keep defaults."""
        if self.session_id is None or not isinstance(self.config_options, list):
            raise ACPError("ACP configuration unavailable")
        matches = [item for item in self.config_options
                   if isinstance(item, dict) and item.get("id") == option_id]
        if len(matches) != 1 or matches[0].get("type") != "select":
            raise ACPError("ACP option unavailable")
        options = matches[0].get("options")
        if not isinstance(options, list):
            raise ACPError("invalid ACP option choices")
        choices = []
        for item in options:
            if not isinstance(item, dict):
                raise ACPError("invalid ACP option choice")
            if "group" in item:
                group = item.get("options")
                if not isinstance(group, list) or any(not isinstance(choice, dict) for choice in group):
                    raise ACPError("invalid ACP option group")
                choices.extend(choice.get("value") for choice in group)
            else:
                choices.append(item.get("value"))
        if not isinstance(value, str) or choices.count(value) != 1:
            raise ACPError("requested ACP configuration is not advertised")
        result = self.request("session/set_config_option", {"sessionId": self.session_id,
                                                            "configId": option_id, "value": value})
        updated = result.get("configOptions")
        if not isinstance(updated, list):
            raise ACPError("ACP selection not confirmed")
        confirmed = [item for item in updated if isinstance(item, dict) and item.get("id") == option_id]
        if len(confirmed) != 1 or confirmed[0].get("currentValue") != value:
            raise ACPError("ACP selection not confirmed")
        self.config_options = updated
        return result

    def prompt(self, text):
        if self.session_id is None or not isinstance(text, str) or not text.strip() or "\0" in text:
            raise ACPError("fresh ACP session and nonempty prompt required")
        result = self.request("session/prompt", {"sessionId": self.session_id,
                                                "prompt": [{"type": "text", "text": text}]})
        # Other stop reasons (limits/cancel/refusal) are not successful completion.
        if self.permission_denied or self.tool_failed or result.get("stopReason") != "end_turn":
            self.failed = True
            raise ACPError("ACP turn did not complete")
        return result
