"""One bounded worker invocation; no control configuration, DB, or automatic retry."""

import argparse
import json
import os
import pwd
import signal
import stat
import sys
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID

from .broker_catalog import proxy_environment
from .broker_client import request_at
from .broker_codex import executor
from .broker_execution_contract import load_at
from .broker_worker import run_one


class WorkerStopped(Exception):
    def __init__(self, signum):
        self.signum = signum


def _directory(value, stack, *, dir_fd=None):
    fd = os.open(value, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=dir_fd)
    stack.callback(os.close, fd)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("private worker-owned directory required")
    return fd


def run(*, socket_path, control_uid, claim_request_id, executable, home, codex_home, workspace_root,
        stop_requested=lambda: None, contract_directory=None, execution_catalog=False, proxy_url=None):
    """Deployment supplies trusted paths. Each claim has a separate workspace.

    Directory metadata checks are not a hostile same-UID sandbox. Protect worker
    parent paths, executable/configuration, control data, and cgroups at deployment.
    """
    if (type(control_uid) is not int or not 0 < control_uid < 4294967295
            or os.geteuid() == 0 or control_uid == os.geteuid()):
        raise ValueError("independent unprivileged identities required")
    if not isinstance(claim_request_id, str) or str(UUID(claim_request_id)) != claim_request_id:
        raise ValueError("canonical claim request ID required")
    if execution_catalog and (contract_directory is None or executable is not None or codex_home is not None or proxy_url is not None):
        raise ValueError("catalog mode supplies executable and agent home")
    paths = [Path(value) for value in (home, workspace_root)]
    if any(not path.is_absolute() or ".." in path.parts for path in paths):
        raise ValueError("absolute deployment paths required")
    home, root = paths
    if home.resolve().is_relative_to(root.resolve()):
        raise ValueError("credential directories must not be inside task workspaces")

    def validate_profile(executable, agent_home):
        if executable is None or agent_home is None:
            raise ValueError("deployment executable and agent home required")
        executable, agent_home = Path(executable), Path(agent_home)
        if any(not path.is_absolute() or ".." in path.parts for path in (executable, agent_home)):
            raise ValueError("absolute deployment paths required")
        binary = executable.stat()
        if (not stat.S_ISREG(binary.st_mode) or binary.st_mode & 0o022
                or binary.st_uid not in (0, os.geteuid()) or not os.access(executable, os.X_OK)):
            raise ValueError("trusted executable required")
        if agent_home.resolve().is_relative_to(root.resolve()):
            raise ValueError("credential directories must not be inside task workspaces")
        return executable, agent_home

    network_environment = proxy_environment(proxy_url)
    if not execution_catalog:
        executable, codex_home = validate_profile(executable, codex_home)
    with ExitStack() as stack:
        contract = None
        catalog = None
        if contract_directory is not None:
            directory = Path(contract_directory)
            if not directory.is_absolute() or ".." in directory.parts:
                raise ValueError("absolute deployment contract directory required")
            contract_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            stack.callback(os.close, contract_fd)
            if execution_catalog:
                from .broker_catalog import Catalog
                catalog = Catalog(contract_fd, control_uid=control_uid, worker_uid=os.geteuid())
                catalog.profiles()
            else:
                contract = load_at(contract_fd, control_uid=control_uid, worker_uid=os.geteuid())
        _directory(home, stack)
        if not execution_catalog:
            _directory(codex_home, stack)
        root_fd = _directory(root, stack)
        def select_contract(inputs):
            nonlocal contract, executable, codex_home, network_environment
            from .broker_execution_contract import validate_selection
            selection = validate_selection(inputs.get("execution"))
            profile = catalog.select(selection["contract_fingerprint"])
            if profile.contract.selection() != selection:
                raise ValueError("task differs from selected deployment")
            contract = profile.contract
            network_environment = proxy_environment(profile.proxy_url)
            executable, codex_home = validate_profile(profile.executable, profile.agent_home)
            _directory(codex_home, stack)
            return contract

        def prepare(inputs, *, task):
            expected = (contract.model, contract.reasoning) if contract else ("gpt-5.6-sol", "medium")
            if (inputs["model"], inputs["reasoning"]) != expected:
                raise ValueError("unsupported deployment model")
            try:
                os.mkdir(claim_request_id, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass  # Reopening a claim workspace never grants permission to rerun.
            _directory(claim_request_id, stack, dir_fd=root_fd)
            workdir = root / claim_request_id
            environment = {"HOME": str(home), "CODEX_HOME": str(codex_home),
                           "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                           "K3_SUPPORT_BROKER_SOCKET": socket_path,
                           "K3_SUPPORT_BROKER_CONTROL_UID": str(control_uid),
                           "K3_SUPPORT_BROKER_TASK": json.dumps(task, separators=(",", ":"))}
            environment.update(network_environment)
            options = {"contract": contract} if contract is not None else {}
            adapter = executor
            if contract is not None and contract.agent == "claude":
                from .broker_claude import executor as claude_executor
                adapter = claude_executor
                del environment["CODEX_HOME"]
                environment["CLAUDE_CONFIG_DIR"] = str(codex_home)
            elif contract is not None and contract.agent == "dsh":
                from .broker_dsh import executor as dsh_executor
                adapter = dsh_executor
                del environment["CODEX_HOME"]
                environment["DSH_HOME"] = str(codex_home)
            elif contract is not None and contract.agent == "opencode":
                from .broker_opencode import executor as opencode_executor
                adapter = opencode_executor
                del environment["CODEX_HOME"]
                environment["K3_SUPPORT_AGENT_HOME"] = str(codex_home)
            elif contract is not None and contract.agent == "hermes":
                from .broker_hermes import executor as hermes_executor
                adapter = hermes_executor
                del environment["CODEX_HOME"]
                environment["K3_SUPPORT_AGENT_HOME"] = str(codex_home)
            return adapter(executable=str(executable), workdir=str(workdir), environment=environment,
                           remote_python=sys.executable, **options)

        def transport(value):
            interrupted = stop_requested()
            if interrupted is not None:
                raise WorkerStopped(interrupted)
            return request_at(socket_path, value, control_uid=control_uid, timeout=5)

        return run_one(claim_request_id=claim_request_id, prepare_executor=prepare, transport=transport,
                       contract_fingerprint=contract.fingerprint if contract else None,
                       executor_agent=contract.agent if contract else "codex",
                       select_contract=select_contract if execution_catalog else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one broker claim; never retry unknown execution automatically")
    parser.add_argument("--socket", required=True)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--control-uid", type=int)
    identity.add_argument("--control-user")
    parser.add_argument("--claim-request-id", required=True)
    parser.add_argument("--agent-executable", "--codex-executable", dest="codex_executable")
    parser.add_argument("--home", required=True)
    parser.add_argument("--agent-home", "--codex-home", dest="codex_home")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--contract-directory")
    parser.add_argument("--execution-catalog", action="store_true")
    parser.add_argument("--proxy-url")
    args = parser.parse_args(argv)
    if args.execution_catalog:
        if not args.contract_directory or args.codex_executable or args.codex_home or args.proxy_url:
            parser.error("catalog mode requires its directory and forbids agent path overrides")
    elif not args.codex_executable or not args.codex_home:
        parser.error("single execution mode requires agent executable and home")

    stopped = []

    def stop(signum, _frame):
        # Do not asynchronously interrupt Popen before its cleanup guard exists.
        # The next bounded RPC/heartbeat check stops and reaps the child group.
        if not stopped:
            stopped.append(signum)

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, stop)
        uid = pwd.getpwnam(args.control_user).pw_uid if args.control_user else args.control_uid
        result = run(socket_path=args.socket, control_uid=uid, claim_request_id=args.claim_request_id,
                     executable=args.codex_executable, home=args.home, codex_home=args.codex_home,
                     workspace_root=args.workspace_root, stop_requested=lambda: stopped[0] if stopped else None,
                     contract_directory=args.contract_directory, execution_catalog=args.execution_catalog, proxy_url=args.proxy_url)
        if stopped:
            raise WorkerStopped(stopped[0])
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except WorkerStopped as error:
        print("Worker interrupted; control-side execution reconciliation required.", file=sys.stderr)
        return 128 + error.signum
    except (OSError, ValueError, TypeError, KeyError):
        # Never print exception text: it may contain task content or credentials.
        print("Worker failed; inspect control state before retrying. No automatic retry performed.", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
