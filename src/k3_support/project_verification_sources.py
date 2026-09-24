"""Control-only source bindings and independently launched source observations."""

import json
import re
import shlex
from importlib.resources import files
from pathlib import PurePosixPath

from .db import transaction
from .execution_transport import plan_argv, target
from .ids import canonical_json, new_id
from .project_bugs import _text
from .project_read_client import ProjectReadError, parse_json
from .timeutil import iso_now


def freeze(conn, config, *, run_id, plan, definition, step, source_paths):
    if (
        config is None
        or not isinstance(source_paths, dict)
        or set(source_paths) != set(step["repositories"])
    ):
        raise ValueError("source paths must cover exactly the step repositories")
    runtime = target(config)
    bindings = []
    case_root = PurePosixPath(config.runtime("remote_worktree_root")) / plan["case_id"]
    for repo in definition["repositories"]:
        if repo["id"] not in source_paths:
            continue
        configured = config.raw["repositories"].get(repo["repository"])
        if (
            configured is None
            or repo["node"] != runtime["host"]
            or step["node"] != runtime["host"]
        ):
            raise ValueError("source repository/node differs from execution deployment")
        path = _text(source_paths[repo["id"]], "source path", 4096)
        parsed = PurePosixPath(path)
        if (
            not parsed.is_absolute()
            or str(parsed) != path
            or ".." in parsed.parts
            or any(ord(c) < 32 for c in path)
            or not (
                path == configured["path"]
                or (parsed != case_root and parsed.is_relative_to(case_root))
            )
        ):
            raise ValueError(
                "source path outside configured repository and Case worktrees"
            )
        bindings.append(
            {
                "repository": repo["repository"],
                "path": path,
                "base_commit": repo["base_commit"],
                "candidate_commit": repo["candidate_commit"],
            }
        )
    conn.execute(
        "INSERT INTO project_verification_sources VALUES(?,?,?)",
        (run_id, canonical_json(bindings), canonical_json(runtime)),
    )


def observe(conn, config, action, *, phase, transport, heartbeat):
    """Launch a fixed sampler, never interpret the worker's command output as one."""
    row = conn.execute(
        "SELECT s.* FROM project_verification_sources s JOIN project_verification_runs v USING(run_id) "
        "WHERE v.remote_request_id=?",
        (action["request_id"],),
    ).fetchone()
    if row is None:
        return None
    if phase not in {"before", "after"}:
        raise ValueError("invalid source observation phase")
    state, payload = "unavailable", {"reason": "source_observation_unavailable"}
    try:
        runtime = json.loads(row["runtime_json"])
        if runtime != target(config):
            raise ValueError("source runtime changed")
        bindings = json.loads(row["bindings_json"])
        source = (
            files("k3_support").joinpath("verification_source_probe.py").read_text()
        )
        command = shlex.join(
            ["/usr/bin/python3", "-I", "-S", "-c", source, row["bindings_json"]]
        )
        heartbeat()
        result = transport(
            argv=plan_argv(runtime, command),
            cwd="/",
            stdin=b"",
            heartbeat=heartbeat,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            timeout=90,
            heartbeat_interval=1,
            output_limit=128000,
            detailed=True,
        )
        if (
            not isinstance(result, dict)
            or type(result.get("exit_code")) is not int
            or result["exit_code"] != 0
            or not isinstance(result.get("stdout"), str)
            or len(result["stdout"].encode()) > 128000
        ):
            raise ValueError("source sampler failed")
        value = parse_json(result["stdout"])
        if (
            not isinstance(value, dict)
            or set(value) != {"sources"}
            or not isinstance(value["sources"], list)
            or len(value["sources"]) != len(bindings)
        ):
            raise ValueError("invalid source observation")
        for expected, actual in zip(bindings, value["sources"], strict=True):
            if (
                not isinstance(actual, dict)
                or set(actual)
                != {
                    "repository",
                    "path",
                    "head",
                    "end_head",
                    "base_is_ancestor",
                    "tracked_content_matches",
                    "tracked_count",
                    "matched",
                    "coverage",
                }
                or any(actual[k] != expected[k] for k in ("repository", "path"))
                or actual["coverage"] != "tracked_source_sample"
                or any(
                    type(actual[k]) is not bool
                    for k in ("base_is_ancestor", "tracked_content_matches", "matched")
                )
                or type(actual["tracked_count"]) is not int
                or not 0 <= actual["tracked_count"] <= 100000
            ):
                raise ValueError("source observation binding changed")
            matched = (
                actual["head"] == actual["end_head"] == expected["candidate_commit"]
                and actual["base_is_ancestor"]
                and actual["tracked_content_matches"]
            )
            if any(
                not isinstance(actual[k], str)
                or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", actual[k])
                for k in ("head", "end_head")
            ):
                raise ValueError("invalid observed object ID")
            if actual["matched"] != matched:
                raise ValueError("source observation is inconsistent")
        payload = value
        state = (
            "matched"
            if all(item["matched"] for item in value["sources"])
            else "mismatch"
        )
    except (OSError, ValueError, TypeError, KeyError, ProjectReadError):
        pass  # Fixed public failure only; no remote stderr or paths from exceptions.
    with transaction(conn):
        conn.execute(
            "INSERT INTO project_verification_observations VALUES(?,?,?,?,?,?)",
            (
                new_id("pvo"),
                row["run_id"],
                phase,
                state,
                canonical_json(payload),
                iso_now(),
            ),
        )
    return state


def projection(conn, run_id):
    return [
        {
            "phase": row["phase"],
            "state": row["state"],
            "recorded_at": row["recorded_at"],
            "sources": json.loads(row["observation_json"]).get("sources", []),
        }
        for row in conn.execute(
            "SELECT phase,state,recorded_at,observation_json FROM project_verification_observations WHERE run_id=? ORDER BY rowid",
            (run_id,),
        )
    ]
