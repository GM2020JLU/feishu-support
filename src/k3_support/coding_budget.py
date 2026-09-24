"""Budget the supervised coding session, not individual internal model turns."""

import os
import re
import stat
import tomllib

from .budget_blocks import record, resolve
from .ids import digest
from .model_budget import BudgetError, invoke


def identity(path):
    # Reject links and non-regular files before a blocking read. This protects
    # the read itself, not the whole provider configuration/runtime boundary.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or info.st_nlink != 1
        ):
            raise BudgetError("coding configuration identity is not trusted")
        raw = stream.read(262145)
        after = os.fstat(stream.fileno())
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise BudgetError("coding configuration changed during identity read")
    if len(raw) > 262144:
        raise BudgetError("coding configuration exceeds limit")
    config = tomllib.loads(raw.decode())
    if config.get("profile"):
        raise BudgetError(
            "selected coding profile requires resolved identity before budgeting"
        )
    provider = config.get("model_provider", "openai")
    if not isinstance(provider, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,100}", provider
    ):
        raise BudgetError("invalid coding provider identity")
    return {"provider": provider, "config_digest": digest(raw.hex())}


def execute(conn, *, job, argv, config_path, transport):
    policy = conn.execute(
        "SELECT * FROM model_budget_policy WHERE singleton=1"
    ).fetchone()
    if policy is None:
        return transport()
    try:
        descriptor = identity(config_path)
    except (OSError, ValueError):
        record(conn, job["job_id"], "run_codex_job", "identity_unverified")
        raise BudgetError(
            "coding provider could not be verified; model not started"
        ) from None
    try:
        result = invoke(
            conn,
            transport=transport,
            before_dispatch=lambda: identity(config_path) == descriptor,
            request_id="coding:" + job["job_id"],
            case_id=job["case_id"],
            provider=descriptor["provider"],
            model=argv[argv.index("-m") + 1],
            amount=policy["attempt_limit"],
            input_digest=digest(
                {"argv": argv, "config_digest": descriptor["config_digest"]}
            ),
        )["value"]
    except BudgetError:
        record(conn, job["job_id"], "run_codex_job", "budget_gate_blocked")
        raise
    if result.returncode == 0:
        resolve(conn, job["job_id"], "run_codex_job")
    else:
        record(conn, job["job_id"], "run_codex_job", "model_result_unconfirmed")
    return result
