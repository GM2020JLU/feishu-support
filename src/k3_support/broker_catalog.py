"""Protected, credential-free execution profiles shared by control and workers."""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .broker_execution_contract import ExecutionContract, load_at, read_object_at


@dataclass(frozen=True)
class Profile:
    name: str
    contract: ExecutionContract
    executable: str
    agent_home: str
    proxy_url: str | None = None


def proxy_environment(value):
    """Only a credential-free proxy route, never arbitrary worker environment."""
    if value is None:
        return {}
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048
            or any(ord(char) <= 32 or ord(char) >= 127 for char in value)):
        raise ValueError("invalid deployment proxy URL")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or parsed.port is None or not 1 <= parsed.port <= 65535):
        raise ValueError("credential-free proxy endpoint with explicit port required")
    environment = {key: value for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                          "http_proxy", "https_proxy", "all_proxy")}
    environment.update(NO_PROXY="localhost,127.0.0.1,::1", no_proxy="localhost,127.0.0.1,::1")
    return environment


class Catalog:
    """Borrow a deployment directory descriptor; reread at authority boundaries."""

    def __init__(self, directory_fd, *, control_uid, worker_uid):
        self.directory_fd = directory_fd
        self.control_uid = control_uid
        self.worker_uid = worker_uid

    def profiles(self):
        value = read_object_at(self.directory_fd, control_uid=self.control_uid,
                               worker_uid=self.worker_uid, name="executors.json")
        if (not isinstance(value, dict) or set(value) != {"version", "profiles"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["profiles"], dict) or not 1 <= len(value["profiles"]) <= 16):
            raise ValueError("invalid execution catalog")
        result, seen = [], set()
        for name, item in value["profiles"].items():
            if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name)
                    or not isinstance(item, dict) or set(item) - {"proxy_url"} != {"contract", "executable", "agent_home"}):
                raise ValueError("invalid execution profile")
            for key in ("executable", "agent_home"):
                path = item[key]
                if (not isinstance(path, str) or not 1 <= len(path) <= 4096
                        or any(ord(c) < 32 for c in path) or not Path(path).is_absolute()
                        or ".." in Path(path).parts or str(Path(path)) != path):
                    raise ValueError("absolute normalized profile path required")
            proxy_environment(item.get("proxy_url"))
            contract = load_at(self.directory_fd, control_uid=self.control_uid,
                               worker_uid=self.worker_uid, name=item["contract"])
            if contract.fingerprint in seen:
                raise ValueError("ambiguous execution catalog fingerprint")
            seen.add(contract.fingerprint)
            result.append(Profile(name, contract, item["executable"], item["agent_home"], item.get("proxy_url")))
        return tuple(result)

    def select(self, fingerprint):
        for profile in self.profiles():
            if profile.contract.fingerprint == fingerprint:
                return profile
        raise ValueError("bound execution profile unavailable")


class ActiveCatalog:
    """Single-slot control reader; an unresolved launch pins every operation.

    No default profile exists. No launch, multiple launches, or removed profiles
    fail closed. This reader does not replace each RPC's grant authorization.
    """

    def __init__(self, conn, catalog):
        self.conn, self.catalog = conn, catalog

    def binding(self):
        rows = self.conn.execute("""SELECT b.*,l.state FROM broker_launches l
            LEFT JOIN broker_launch_bindings b USING(claim_request_id)
            WHERE l.state!='finished' LIMIT 2""").fetchall()
        if len(rows) != 1 or rows[0]["job_id"] is None:
            raise ValueError("one bound active launch required")
        return rows[0]

    def __call__(self):
        binding = self.binding()
        contract = self.catalog.select(binding["contract_fingerprint"]).contract
        if contract.agent != binding["agent"]:
            raise ValueError("launch agent changed")
        return contract

    def check_claim(self, request_id):
        if self.binding()["claim_request_id"] != request_id:
            raise ValueError("claim not dispatched")
