"""Control-account-only Meegle read boundary; not a worker or public RPC.

Uses the official CLI contract researched at 30aa38ef66ce47d232a84fbd731dbc156ac933a7.
Business payloads stay raw until actual endpoint schemas have been accepted.
No method claims complete pagination, effective write permission or CAS support.
Deployment owns the binary, profile and account home; callers must check read scope.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .bounded_cli import OutputLimitError, run
from .timeutil import iso_now


class ProjectReadError(RuntimeError):
    """Stable, safe error code. Never includes raw CLI output or parameters."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _string(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 2048
        and value == value.strip()
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def _page(value):
    return type(value) is int and 1 <= value <= 100000


def _strings(value):
    return (
        isinstance(value, list)
        and 1 <= len(value) <= 200
        and all(_string(v) for v in value)
        and len(value) == len(set(value))
    )


# Exact documented read operations, not a prefix-based "get/list" heuristic.
# Tuple = required keys, optional keys. New commands need an explicit review.
READS = {
    "project.search": ({"project_key", "page_num"}, set()),
    "user.me": (set(), set()),
    "user.search": ({"project_key", "user_keys"}, set()),
    "workitem.meta-types": ({"project_key"}, set()),
    "workitem.meta-fields": (
        {"project_key", "work_item_type", "page_num"},
        {"field_keys", "field_query", "field_types"},
    ),
    "workitem.meta-roles": (
        {"project_key", "work_item_type", "page_num"},
        {"role_keys", "role_query"},
    ),
    "workitem.meta-create-fields": ({"project_key", "work_item_type"}, set()),
    "workitem.get": (
        {"project_key", "work_item_id", "fields"},
        {"page_size", "page_token"},
    ),
    "workitem.list-op-records": ({"project_key", "work_item_id"}, {"start", "end", "start_from"}),
    "workflow.list-state-transitions": (
        {"project_key", "work_item_id", "work_item_type", "user_key"},
        set(),
    ),
    "workflow.list-state-required": (
        {"project_key", "work_item_id", "state_key"},
        {"mode"},
    ),
    "workflow.get-node": (
        {"project_key", "work_item_id", "page_num"},
        {"node_id_list", "field_key_list", "need_sub_task"},
    ),
    "comment.list": ({"project_key", "work_item_id", "page_num"}, {"end_time"}),
    "relation.meta-definitions": (
        {"project_key", "work_item_type"},
        {"relation_work_item_type"},
    ),
    "relation.list": (
        {"project_key", "work_item_id", "page_num", "relation_id"},
        {"page_size", "relation_field_key", "node_id"},
    ),
}
ARRAY_KEYS = {
    "field_keys",
    "field_types",
    "role_keys",
    "fields",
    "node_id_list",
    "field_key_list",
}


def _params(command, params):
    if (
        not isinstance(command, str)
        or command not in READS
        or not isinstance(params, dict)
    ):
        raise ProjectReadError("invalid_read_request")
    required, optional = READS[command]
    if not required <= params.keys() or params.keys() - required - optional:
        raise ProjectReadError("invalid_read_request")
    for key, value in params.items():
        if key == "user_keys":
            valid = _strings(value) and len(value) <= 20
        elif key in ARRAY_KEYS:
            valid = _strings(value)
        elif key == "page_num":
            valid = _page(value)
        elif key == "page_size":
            valid = type(value) is int and 1 <= value <= 200
        elif key in {"start", "end", "end_time"}:
            valid = type(value) is int and 0 <= value <= 253402300799999
        elif key == "need_sub_task":
            valid = type(value) is bool
        elif key == "mode":
            valid = value == "unfinished"
        else:
            valid = _string(value)
        if not valid:
            raise ProjectReadError("invalid_read_request")
    if (command == "workitem.list-op-records" and {"start", "end"} & params.keys()
        and (not {"start", "end"} <= params.keys() or not 0 < params["start"] < params["end"])):
        raise ProjectReadError("invalid_read_request")
    if command == "workitem.get":
        fields = params["fields"]
        if ("_all" in fields and fields != ["_all"]) or (
            {"page_size", "page_token"} & params.keys() and fields != ["_all"]
        ):
            raise ProjectReadError("invalid_read_request")
    return json.dumps(
        params, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def canonical_host(value):
    """Host only, explicit port preserved; never accepts credentials or a URL."""
    if not _string(value) or re.search(r"[/@?#\\\s]", value):
        raise ProjectReadError("invalid_host")
    match = re.fullmatch(r"([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?", value)
    if not match:
        raise ProjectReadError("invalid_host")
    domain, port = match.groups()
    domain = domain.lower().rstrip(".")
    if (
        not domain
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in domain.split(".")
        )
        or (port is not None and not 1 <= int(port) <= 65535)
    ):
        raise ProjectReadError("invalid_host")
    return domain + (":" + port if port is not None else "")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("non-finite JSON")


def parse_json(text):
    try:
        value = json.loads(
            text, object_pairs_hook=_object, parse_constant=_invalid_constant
        )
    except (ValueError, TypeError, RecursionError):
        raise ProjectReadError("invalid_cli_response") from None
    if not isinstance(value, (dict, list)):
        raise ProjectReadError("invalid_cli_response")
    return value


@dataclass(frozen=True)
class AuthStatus:
    state: str  # authenticated, login_required, rejected, unavailable
    host: str | None
    expires_in_minutes: int | None = None


class MeegleReadClient:
    def __init__(self, *, executable, sha256, profile, host, runner=run):
        self.executable = Path(executable)
        if (
            not self.executable.is_absolute()
            or not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or not isinstance(profile, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile)
        ):
            raise ProjectReadError("invalid_client_configuration")
        self.sha256 = sha256
        self.profile = profile
        self.host = canonical_host(host)
        self.runner = runner

    def _invoke(self, args):
        # Deployment protects binary AND its parents from worker writes. Recheck
        # each invocation to reject updates until their version is accepted.
        try:
            info = self.executable.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o022
                or not info.st_mode & 0o111
                or info.st_uid not in {0, os.geteuid()}
                or not 0 < info.st_size <= 128 * 1024 * 1024
            ):
                raise ProjectReadError("untrusted_client_binary")
            with self.executable.open("rb") as source:
                actual = hashlib.file_digest(source, "sha256").hexdigest()
            if actual != self.sha256:
                raise ProjectReadError("client_binary_changed")
        except OSError:
            raise ProjectReadError("client_unavailable") from None
        # Preserve this service account's home, never borrow a worker/personal
        # profile. Exclude token env vars, loader injection and shell startup.
        # The session-bus variables stay: CLI tokens may live in the account's
        # own SecretService keyring, which is unreachable without them. USER
        # and LOGNAME are identity labels the CLI folds into its file-credential
        # machine key; without USER decryption fails as "no local token".
        keys = {
            "HOME",
            "PATH",
            "LANG",
            "LC_ALL",
            "TZ",
            "USER",
            "LOGNAME",
            "DBUS_SESSION_BUS_ADDRESS",
            "XDG_RUNTIME_DIR",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "https_proxy",
            "http_proxy",
            "all_proxy",
            "no_proxy",
        }
        env = {key: value for key, value in os.environ.items() if key in keys}
        argv = [
            str(self.executable),
            "--profile",
            self.profile,
            *args,
            "--format",
            "json",
        ]
        try:
            return self.runner(
                argv,
                env=env,
                timeout=30,
                stdout_limit=4 * 1024 * 1024,
                stderr_limit=128 * 1024,
            )
        except subprocess.TimeoutExpired:
            raise ProjectReadError("read_timeout") from None
        except OutputLimitError:
            raise ProjectReadError("response_too_large") from None
        except (OSError, UnicodeError):
            raise ProjectReadError("client_execution_failed") from None

    def auth_status(self):
        result = self._invoke(["auth", "status"])
        payload = parse_json(result.stdout)
        if (
            not isinstance(payload, dict)
            or type(payload.get("authenticated")) is not bool
        ):
            raise ProjectReadError("invalid_cli_response")
        raw_host = payload.get("host")
        host = canonical_host(raw_host) if raw_host is not None else None
        if host is not None and host != self.host:
            raise ProjectReadError("profile_host_mismatch")
        if payload["authenticated"]:
            expiry = payload.get("expires_in_minutes")
            if (
                result.returncode != 0
                or host is None
                or (expiry is not None and (type(expiry) is not int or expiry < 0))
            ):
                raise ProjectReadError("invalid_cli_response")
            return AuthStatus("authenticated", host, expiry)
        reason = payload.get("reason")
        if (
            result.returncode == 1
            and isinstance(reason, str)
            and reason in {"no local token", "token rejected by server"}
        ):
            return AuthStatus(
                "login_required" if reason == "no local token" else "rejected", host
            )
        if (
            result.returncode == 2
            and isinstance(reason, str)
            and (
                reason == "server unreachable"
                or reason.startswith("server unreachable: ")
            )
        ):
            return AuthStatus("unavailable", host)
        raise ProjectReadError("invalid_cli_response")

    def _guard(self):
        status = self.auth_status()
        if status.state != "authenticated":
            raise ProjectReadError("auth_" + status.state)

    def _success(self, args):
        result = self._invoke(args)
        if result.returncode != 0:
            # Don't guess error types from private upstream message strings.
            raise ProjectReadError("remote_read_failed")
        value = parse_json(result.stdout)
        if isinstance(value, dict) and value.get("error") is not None:
            raise ProjectReadError("remote_read_failed")
        return value

    def prepare_attachment_download(self, destination, source_reference):
        if (not isinstance(destination, dict)
                or set(destination) != {"host", "project_key", "type_key", "item_id"}
                or not all(_string(v) for v in destination.values())
                or destination["host"] != self.host or not _string(source_reference)):
            raise ProjectReadError("invalid_read_request")
        self._guard()
        return self._success(["attachment", "prepare-download", "--params", json.dumps({
            "project_key": destination["project_key"], "work_item_id": destination["item_id"],
            "file_url": source_reference}, ensure_ascii=False)])

    def decode_workitem_url(self, url):
        if not _string(url):
            raise ProjectReadError("invalid_workitem_url")
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or canonical_host(parsed.netloc) != self.host
            ):
                raise ValueError()
        except (ValueError, ProjectReadError):
            raise ProjectReadError("invalid_workitem_url") from None
        value = self._success(["url", "decode", "--url", url])
        if (
            not isinstance(value, dict)
            or value.get("url_kind") != "workitem_detail"
            or value.get("is_resource", False) is not False
            or canonical_host(value.get("host")) != self.host
            or not all(
                _string(value.get(k))
                for k in ("simple_name", "work_item_type", "work_item_id")
            )
        ):
            raise ProjectReadError("unsupported_workitem_url")
        # Do not return raw URL/query/diagnostic fields or treat slug as a key.
        return {
            key: value[key] for key in ("simple_name", "work_item_type", "work_item_id")
        } | {
            "host": self.host,
            "project_key_resolved": False,
        }

    def inspect(self, command):
        if not isinstance(command, str) or command not in READS:
            raise ProjectReadError("invalid_read_request")
        self._guard()
        # Native 1.0.23 prints help text here even with --format json. Keep
        # human documentation separate from machine-readable business schemas.
        result = self._invoke(["--refresh", "inspect", command])
        if result.returncode != 0:
            raise ProjectReadError("remote_read_failed")
        text = result.stdout.strip()
        heading = "meegle " + command.replace(".", " ")
        if (
            not text
            or text.splitlines()[0].strip() != heading
            or any(ord(c) < 32 and c not in "\n\r\t" for c in text)
            or "\x7f" in text
        ):
            raise ProjectReadError("invalid_cli_response")
        return {
            "command": command,
            "format": "text",
            "text": text,
            "machine_schema": False,
        }

    def query_bugs(self, scope, *, keyword="", after_id=0):
        from .project_bug_query import compile_query, normalize

        params = compile_query(scope, keyword=keyword, after_id=after_id)
        self._guard()
        payload = self._success([
            "workitem", "query", "--params",
            json.dumps(params, ensure_ascii=False, allow_nan=False),
        ])
        return normalize(payload, scope, after_id=after_id) | {
            "host": self.host, "observed_at": iso_now(),
        }

    def read_page(self, command, params):
        encoded = _params(command, params)
        if len(encoded.encode("utf-8")) > 65536:
            raise ProjectReadError("invalid_read_request")
        self._guard()
        resource, method = command.split(".")
        payload = self._success([resource, method, "--params", encoded])
        return {
            "command": command,
            "host": self.host,
            "observed_at": iso_now(),
            "completeness": "page_only",
            "payload": payload,
        }
