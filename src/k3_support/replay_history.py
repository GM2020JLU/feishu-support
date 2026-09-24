"""Replay a new event against an existing snapshot, not a past-time simulation."""

import copy
import fcntl
import json
import math
import os
import sqlite3
import sysconfig
from pathlib import Path

from .broker_process import run_process
from .config import Config, validate_config
from .ids import digest
from .replay_sandbox import sandbox_command
from .replay_snapshot import replay_snapshot
from .routing import validate_route_output
from .workflow_replay import replay_inbound

# Linux UAPI values: some bundled Python builds omit the symbolic constants.
F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
F_SEAL_WRITE = getattr(fcntl, "F_SEAL_WRITE", 8)
ALL_SEALS = F_SEAL_WRITE | 1 | 2 | 4


def execute_snapshot(
    request: dict, path: str = "/replay/snapshot.db", *, probe=False
) -> dict:
    required = {"config", "event"} if probe else {"config", "event", "proposal"}
    if not isinstance(request, dict) or set(request) - {'assumptions'} != required:
        raise ValueError("snapshot replay requires config, event and supplied proposal")
    from .replay_assumptions import validate, apply, clock
    assumptions = validate(request.get('assumptions', {}))
    raw = copy.deepcopy(request["config"])
    raw["paths"] = {"database": "/tmp/replay.db", "data_dir": "/tmp/replay"}
    config = Config(validate_config(raw), Path("/tmp/replay.yaml"))
    proposal = None if probe else validate_route_output(request["proposal"])
    routing_inputs = []

    def router(value):
        routing_inputs.append(copy.deepcopy(value))
        return copy.deepcopy(proposal)

    # The source is a sealed, coherent SQLite backup, not a live WAL database.
    memory = sqlite3.connect(":memory:", isolation_level=None)
    try:
        with open(path, "rb") as stream:
            data = stream.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("snapshot exceeds transfer limit")
        memory.deserialize(data)
        memory.row_factory = sqlite3.Row
        memory.execute("PRAGMA foreign_keys=ON")
        with clock(assumptions):
            from .notification_schedule import window
            from .timeutil import utc_now
            business_window = window(config, utc_now())
            apply(memory, config, request['event'], assumptions)
            result = replay_inbound(memory, config, request["event"], message_router=router)
        return {
            "report": result,
            "model_invoked": False,
            "scope": "new_event_against_current_snapshot_not_historical_time_travel",
            "external_consumers": False,
            "routing_inputs": routing_inputs,
            "assumptions": assumptions,
            "clock_scope": "context_local_business_clock_not_historical_snapshot_or_transport_deadlines",
            "business_window": business_window,
        }
    finally:
        memory.close()


def run_snapshot_replay(database: Path, request: dict, *, timeout: float = 30) -> dict:
    """Transfer a <=32 MiB snapshot through sealed Linux memory, never a temp DB.

    The source file is not mounted in the sandbox. The supplied proposal is a
    fixture, not a model inference. Returned intentions can contain private text.
    """
    _validate_timeout(timeout)
    _payload(request)
    return _run_snapshot_data(_snapshot_data(database), request, timeout=timeout)


def _validate_timeout(timeout):
    if (
        type(timeout) not in {int, float}
        or not math.isfinite(timeout)
        or not 0 < timeout <= 300
    ):
        raise ValueError("timeout must be finite and in (0, 300]")


def _payload(request):
    payload = json.dumps(request, allow_nan=False).encode()
    if len(payload) > 262144:
        raise ValueError("snapshot replay request exceeds input limit")
    return payload


def _snapshot_data(database, *, upgrade_schema=True, timeout_seconds=30):
    with replay_snapshot(database, max_bytes=32 * 1024 * 1024,
                         upgrade_schema=upgrade_schema, timeout_seconds=timeout_seconds) as memory:
        data = memory.serialize()
    # sqlite3_deserialize cannot open WAL-mode images. Backup already folded
    # committed WAL pages into this self-contained image; change only its
    # read/write format flags to rollback mode, never the original database.
    if data[:16] != b"SQLite format 3\0":
        raise ValueError("invalid SQLite snapshot header")
    data = data[:18] + b"\x01\x01" + data[20:]
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("upgraded snapshot exceeds transfer limit")
    return data


def run_snapshot_inference(
    database: Path, request: dict, *, router, timeout=30
) -> dict:
    """Call a trusted router with production observations, never evaluation labels.

    The caller owns the router's deadline and transport authorization. This
    library never infers provider identity or billing from callback success.
    Both sandbox phases use the identical frozen snapshot. The router runs
    outside the sandbox and receives only the normal routing input, not the DB.
    """
    _validate_timeout(timeout)
    if not isinstance(request, dict) or set(request) - {'assumptions'} != {"config", "event"}:
        raise ValueError(
            "inference request accepts only config and event, never expected labels"
        )
    from .replay_assumptions import validate
    validate(request.get('assumptions', {}))
    request = json.loads(_payload(request))
    data = _snapshot_data(database)
    probe = _run_snapshot_data(data, request, timeout=timeout, probe=True)
    inputs = probe.pop("routing_inputs")
    if not inputs:
        return {
            **probe,
            "model_callback_invoked": False,
            "inference_scope": "routing_not_requested",
        }
    if len(inputs) != 1:
        raise ValueError("expected exactly one routing inference request")
    input_digest = digest(inputs[0])
    proposal = validate_route_output(router(copy.deepcopy(inputs[0])))
    result = _run_snapshot_data(
        data, {**request, "proposal": proposal}, timeout=timeout
    )
    applied_inputs = result.pop("routing_inputs")
    if len(applied_inputs) != 1 or digest(applied_inputs[0]) != input_digest:
        raise ValueError(
            "routing observations changed between inference and application"
        )
    return {
        **result,
        "model_callback_invoked": True,
        "inference_scope": "message_routing_only_not_full_model_workflow",
        "model_invoked": None,
        "provider_verification": "not_established_by_callback",
        "routing_input_digest": input_digest,
        "proposal_digest": digest(proposal),
    }


def _run_snapshot_data(data, request, *, timeout, probe=False, pipeline=False, debug=False,
                       package=None, site_packages=None):
    if debug and (pipeline or probe):
        raise ValueError('snapshot execution modes are mutually exclusive')
    payload = _payload(request)
    fd = os.memfd_create("k3-replay-snapshot", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("snapshot transfer stalled")
            view = view[written:]
        fcntl.fcntl(
            fd,
            F_ADD_SEALS,
            ALL_SEALS,
        )
        os.lseek(fd, 0, os.SEEK_SET)
        program = (
            "import resource,json,sys\n"
            "resource.setrlimit(resource.RLIMIT_AS,(1073741824,1073741824))\n"
            "resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n"
            "from k3_support.replay_history import execute_snapshot\n"
            f"print(json.dumps(execute_snapshot(json.load(sys.stdin),probe={probe!r}),ensure_ascii=False))"
        )
        if pipeline:
            program = program.replace('from k3_support.replay_history import execute_snapshot',
                                      'from k3_support.replay_model_pipeline import execute')
            program = program.replace(f'execute_snapshot(json.load(sys.stdin),probe={probe!r})',
                                      'execute(json.load(sys.stdin))')
        if debug:
            program = program.replace('from k3_support.replay_history import execute_snapshot',
                                      'from k3_support.replay_debug_snapshot import execute')
            program = program.replace(f'execute_snapshot(json.load(sys.stdin),probe={probe!r})',
                                      'execute(json.load(sys.stdin))')
        argv = sandbox_command(
            package=Path(__file__).parent if package is None else Path(package),
            site_packages=Path(sysconfig.get_paths()["purelib"]) if site_packages is None else Path(site_packages),
            program=program,
        )
        separator = argv.index("--proc")
        argv[separator:separator] = [
            "--ro-bind-data",
            str(fd),
            "/replay/snapshot.db",
        ]
        output = run_process(
            argv=argv,
            cwd="/",
            env={},
            stdin=payload,
            heartbeat=lambda: None,
            timeout=timeout,
            heartbeat_interval=min(1, timeout),
            output_limit=1048576,
            pass_fds=(fd,),
        )
        result = json.loads(output)
        if not isinstance(result, dict):
            raise TypeError("invalid snapshot replay output")
        return result
    finally:
        os.close(fd)
