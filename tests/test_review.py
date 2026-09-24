from __future__ import annotations

import copy
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path

import pytest

from k3_support.config import Config, validate_config
from k3_support.control import ControlMessage, execute_control
from k3_support.executors import (
    BoardExecutor,
    ExecutionResult,
    ExecutorError,
    create_codex_job,
    record_codex_result,
)
from k3_support.review import (
    ReviewError,
    fail_wip_push_job,
    handle_codex_completion,
    prepare_codex_review,
    run_hermes_review,
    run_wip_push_job,
    validate_codex_manifest,
    verify_codex_manifest,
)
from k3_support.store import claim_jobs, create_case, ingest_event, transition_case

HEAD = "a" * 40
OUTPUT_HASH = "b" * 64


def active_config(config, **features) -> Config:
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"]["codex"] = True
    raw["features"].update(features)
    raw["repositories"] = {
        "u-boot": {
            "path": "/data/home2/operator/WorkSpace/k3/uboot-2022.10",
            "remote": "origin",
            "base_branch": "main",
        }
    }
    return Config(validate_config(raw), config.path)


def result_text(
    case_id: str, *, head: str = HEAD, requested_actions: list[dict] | None = None
) -> str:
    root = f"/data/home2/operator/WorkSpace/k3-ai-worktrees/{case_id}/u-boot"
    manifest = {
        "schema_version": 1,
        "repositories": [
            {
                "repo": "u-boot",
                "worktree": root,
                "head_commit": head,
                "commits": [head],
                "dirty": False,
            }
        ],
        "checks": [
            {
                "name": "unit",
                "layer": "build",
                "repo": "u-boot",
                "command": ["make", "check"],
                "exit_code": 0,
                "output_path": f"{root}/.k3-support/unit.log",
                "output_sha256": OUTPUT_HASH,
            }
        ],
        "requested_actions": requested_actions or [],
    }
    sections = {
        "status": "completed",
        "root_cause": "Verified test root cause.",
        "changes": "One Case-scoped local commit.",
        "verification": "Static Git checks and recorded build passed.",
        "board_state": "not used",
        "push_state": "not pushed",
        "artifacts": json.dumps(manifest, sort_keys=True),
        "risks": "Board behavior was not tested.",
        "next_action": "Reply with the verified limitation.",
        "reply_draft": "[AI 自动回复]已完成静态和构建检查；尚未上板。",
    }
    return "\n".join(f"## {name}\n{value}" for name, value in sections.items())


def make_job(
    conn, cfg, *, with_source: bool = True, requested_actions: list[dict] | None = None
):
    event_pk = None
    if with_source:
        event_pk, _ = ingest_event(
            conn,
            source="feishu_user_poll",
            identity="user",
            external_id="om_review",
            payload={"content": "u-boot bug"},
            occurred_at=datetime.now(UTC).isoformat(),
            sender_id="ou_colleague",
            chat_id="oc_support",
        )
    case_id, _ = create_case(
        conn,
        title="u-boot bug",
        case_type="bug",
        severity="P2",
        confidence=0.6,
        requester_id="ou_colleague" if with_source else None,
        requester_chat_id="oc_support" if with_source else None,
        source_event_pk=event_pk,
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="classified",
        expected_version=1,
    )
    transition_case(
        conn,
        case_id=case_id,
        after="investigating",
        actor_type="system",
        actor_id=None,
        reason="delegated",
        expected_version=2,
    )
    brief = (
        "# UNTRUSTED INPUT\nx\n# FORBIDDEN ACTIONS\nNo push\n# ACCEPTANCE TESTS\nBuild"
    )
    job_id, _ = create_codex_job(conn, cfg, case_id=case_id, brief=brief, repo="u-boot")
    job = conn.execute("SELECT workdir FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    Path(job["workdir"], "codex-final.md").write_text(
        result_text(case_id, requested_actions=requested_actions), encoding="utf-8"
    )
    conn.execute(
        "UPDATE jobs SET state='succeeded',output_digest='agent-output' WHERE job_id=?",
        (job_id,),
    )
    record_codex_result(conn, job_id=job_id)
    return case_id, job_id


def remote_runner(expected_head: str = HEAD):
    def run(argv, cwd, timeout):
        assert argv[:2] == ["ssh", "buildhost"]
        remote_argv = shlex.split(argv[2])
        if "realpath" in remote_argv:
            stdout = remote_argv[-1] + "\n"
        elif "--show-toplevel" in remote_argv:
            stdout = remote_argv[remote_argv.index("-C") + 1] + "\n"
        elif "get-url" in remote_argv:
            stdout = "ssh://operator@gerrit.example.com:29418/uboot/uboot\n"
        elif "ls-remote" in remote_argv:
            stdout = "0" * 40 + "\trefs/heads/main\n"
        elif "rev-list" in remote_argv:
            stdout = expected_head + " " + "0" * 40 + "\n"
        elif "rev-parse" in remote_argv:
            stdout = expected_head + "\n"
        elif "status" in remote_argv:
            stdout = ""
        elif "show" in remote_argv:
            path = remote_argv[remote_argv.index("-C") + 1]
            case_id = next(part for part in path.split("/") if part.startswith("K3-"))
            stdout = f"fix: {case_id}\n"
        elif "sha256sum" in remote_argv:
            stdout = OUTPUT_HASH + "  artifact\n"
        else:
            stdout = ""
        return ExecutionResult(argv, 0, stdout, "")

    return run


def test_manifest_rejects_paths_outside_case(config):
    cfg = active_config(config)
    case_id = "K3-20260901-0001"
    value = json.loads(
        json.loads(json.dumps(result_text(case_id)))
        .split("## artifacts\n", 1)[1]
        .split("\n## risks", 1)[0]
    )
    value["checks"][0]["output_path"] = "/tmp/forged.log"
    with pytest.raises(ReviewError, match="outside the Case root"):
        validate_codex_manifest(
            json.dumps(value),
            case_id=case_id,
            configured_repositories=set(cfg.raw["repositories"]),
        )


def test_manifest_review_uses_configured_remote_root_and_host(config):
    case_id = "K3-20260901-0001"
    root = f"/srv/firmware/worktrees/{case_id}/u-boot"
    raw = copy.deepcopy(config.raw)
    raw["runtime"].update(
        {
            "remote_host": "builder@k3-host",
            "remote_workspace_root": "/srv/firmware",
            "remote_source_root": "/srv/firmware/source",
            "remote_worktree_root": "/srv/firmware/worktrees",
            "ssh_command": "/opt/ssh/bin/ssh",
        }
    )
    raw["repositories"] = {
        "u-boot": {
            "path": "/srv/firmware/source/u-boot",
            "remote": "origin",
            "base_branch": "main",
        }
    }
    cfg = Config(validate_config(raw), config.path)
    manifest = {
        "schema_version": 1,
        "repositories": [
            {
                "repo": "u-boot",
                "worktree": root,
                "head_commit": HEAD,
                "commits": [HEAD],
                "dirty": False,
            }
        ],
        "checks": [
            {
                "name": "build",
                "layer": "build",
                "repo": "u-boot",
                "command": ["make"],
                "exit_code": 0,
                "output_path": f"{root}/.k3-support/build.log",
                "output_sha256": OUTPUT_HASH,
            }
        ],
        "requested_actions": [],
    }
    validated = validate_codex_manifest(
        json.dumps(manifest),
        case_id=case_id,
        configured_repositories={"u-boot"},
        remote_worktree_root=cfg.runtime("remote_worktree_root"),
    )
    calls = []

    def runner(argv, cwd, timeout):
        calls.append(argv)
        remote_argv = shlex.split(argv[2])
        if "realpath" in remote_argv:
            stdout = remote_argv[-1] + "\n"
        elif "--show-toplevel" in remote_argv:
            stdout = remote_argv[remote_argv.index("-C") + 1] + "\n"
        elif "rev-parse" in remote_argv:
            stdout = HEAD + "\n"
        elif "show" in remote_argv:
            stdout = f"fix: {case_id}\n"
        elif "sha256sum" in remote_argv:
            stdout = OUTPUT_HASH + "  artifact\n"
        else:
            stdout = ""
        return ExecutionResult(argv, 0, stdout, "")

    checks = verify_codex_manifest(
        cfg, case_id=case_id, manifest=validated, runner=runner
    )
    assert checks
    assert all(call[:2] == ["/opt/ssh/bin/ssh", "builder@k3-host"] for call in calls)
    assert all("/data/home2/operator" not in call[2] for call in calls)


def test_prepare_review_independently_checks_and_records_evidence(conn, config):
    cfg = active_config(config)
    case_id, job_id = make_job(conn, cfg)
    result = prepare_codex_review(conn, cfg, job_id=job_id, runner=remote_runner())
    assert result["status"] == "verified"
    assert len(result["evidence_ids"]) == 1
    recorded = next(item for item in result["checks"] if item["kind"] == "recorded_check")
    assert recorded["artifact_verified"] is True
    assert recorded["execution_verified"] is False and recorded["verified"] is False
    assert (
        prepare_codex_review(conn, cfg, job_id=job_id, runner=remote_runner())[
            "review_id"
        ]
        == result["review_id"]
    )
    rows = conn.execute(
        """SELECT e.evidence_layer,cs.requester_access FROM evidence e
           JOIN case_sources cs USING(source_id) WHERE e.case_id=? ORDER BY e.evidence_layer""",
        (case_id,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [("static", "allowed")]


@pytest.mark.parametrize("reported_exit", [0, 1, -1])
def test_reported_exit_and_matching_log_never_become_test_proof(config, reported_exit):
    cfg = active_config(config)
    case_id = "K3-20260901-0001"
    text = result_text(case_id)
    manifest = json.loads(text.split("## artifacts\n", 1)[1].split("\n## risks", 1)[0])
    manifest["checks"][0]["exit_code"] = reported_exit
    checks = verify_codex_manifest(cfg, case_id=case_id, manifest=manifest, runner=remote_runner())
    check = next(c for c in checks if c["kind"] == "recorded_check")
    assert check["output_sha256"] == OUTPUT_HASH
    assert check["exit_code"] == reported_exit
    assert check["exit_code_source"] == "model_report"
    assert check["artifact_verified"] is True
    assert check["verified"] is False and check["execution_verified"] is False


def test_cached_legacy_pass_cannot_be_replayed_or_silently_rewritten(conn, config):
    cfg = active_config(config)
    _, job_id = make_job(conn, cfg)
    bundle = prepare_codex_review(conn, cfg, job_id=job_id, runner=remote_runner())
    old_checks = bundle["checks"]
    recorded = next(c for c in old_checks if c["kind"] == "recorded_check")
    recorded["verified"] = True
    for key in ("artifact_verified", "execution_verified", "exit_code_source"):
        recorded.pop(key)
    old_json = json.dumps(old_checks)
    conn.execute("UPDATE codex_reviews SET independent_checks_json=? WHERE job_id=?", (old_json, job_id))
    with pytest.raises(ReviewError, match="legacy review"):
        prepare_codex_review(conn, cfg, job_id=job_id, runner=remote_runner())
    assert conn.execute("SELECT independent_checks_json FROM codex_reviews WHERE job_id=?", (job_id,)).fetchone()[0] == old_json


def test_boolean_exit_is_not_a_valid_reported_exit(config):
    cfg = active_config(config)
    case_id = "K3-20260901-0001"
    value = json.loads(result_text(case_id).split("## artifacts\n", 1)[1].split("\n## risks", 1)[0])
    value["checks"][0]["exit_code"] = False
    with pytest.raises(ReviewError, match="integer"):
        validate_codex_manifest(json.dumps(value), case_id=case_id, configured_repositories=set(cfg.raw["repositories"]))


def test_prepare_review_rejects_manifest_when_remote_head_changed(conn, config):
    cfg = active_config(config)
    _, job_id = make_job(conn, cfg)
    result = prepare_codex_review(
        conn, cfg, job_id=job_id, runner=remote_runner("c" * 40)
    )
    assert result["status"] == "rejected"
    assert result["evidence_ids"] == []
    assert "HEAD changed" in result["error"]


def test_hermes_review_can_only_reply_with_verified_bundle(conn, config):
    cfg = active_config(config)
    cfg.raw["coordination"]["work_hours_send_grace_seconds"] = 0
    cfg.raw["coordination"]["off_hours_send_grace_seconds"] = 0
    case_id, job_id = make_job(conn, cfg)
    bundle = prepare_codex_review(conn, cfg, job_id=job_id, runner=remote_runner())
    case = conn.execute(
        "SELECT version FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()

    def hermes_runner(argv, prompt, timeout):
        decision = {
            "decision_id": f"hermes-review-{bundle['review_id']}",
            "case_id": case_id,
            "expected_case_version": case["version"],
            "intent": "reply",
            "confidence": 0.9,
            "evidence_ids": bundle["evidence_ids"],
            "reply_draft": "[AI 自动回复]已核对 Git 静态信息；构建结果尚未独立验证，尚未上板。",
            "proposed_actions": [
                {
                    "type": "feishu_reply",
                    "source_event_pk": conn.execute(
                        "SELECT event_pk FROM inbound_events"
                    ).fetchone()[0],
                    "source_message_id": "om_review",
                }
            ],
            "facts": ["independent Git checks passed"],
            "inferences": [],
            "unknowns": ["build execution is unverified", "board behavior is untested"],
        }
        return ExecutionResult(argv, 0, json.dumps(decision), "")

    result = run_hermes_review(
        conn,
        cfg,
        job_id=job_id,
        remote_runner=remote_runner(),
        hermes_runner=hermes_runner,
    )
    assert result["applied"]["applied"] is True
    assert result["knowledge_candidate_id"].startswith("knw_")
    outbox = conn.execute(
        "SELECT action_type,payload_json FROM outbox WHERE case_id=?", (case_id,)
    ).fetchone()
    assert outbox["action_type"] == "reply"
    assert json.loads(outbox["payload_json"])["reply_basis"] == "verified_evidence"
    # Exercise the actual reviewed-decision exception, not just an Outbox label.
    # All remote evidence and Feishu transports remain explicit local doubles.
    from k3_support.delivery import claim_outbox, deliver_claimed
    from k3_support.lark import CommandResult

    receipt = deliver_claimed(
        conn, cfg, claim_outbox(conn, worker_id="review-fixture-sender"),
        lark_runner=lambda _: CommandResult({"message_id": "om_reviewed_answer"}, "user", []),
    )
    assert receipt.remote_id == "om_reviewed_answer"
    assert (
        conn.execute(
            "SELECT status FROM codex_reviews WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        == "decision_applied"
    )
    knowledge = conn.execute(
        """SELECT status,canonical_case_id,evidence_layers_json
           FROM knowledge_entries WHERE knowledge_id=?""",
        (result["knowledge_candidate_id"],),
    ).fetchone()
    assert knowledge["status"] == "candidate"
    assert knowledge["canonical_case_id"] == case_id
    assert json.loads(knowledge["evidence_layers_json"]) == ["static"]
    assert (
        conn.execute(
            "SELECT count(*) FROM knowledge_sources WHERE knowledge_id=?",
            (result["knowledge_candidate_id"],),
        ).fetchone()[0]
        == 1
    )
    duplicate = run_hermes_review(
        conn,
        cfg,
        job_id=job_id,
        remote_runner=remote_runner(),
        hermes_runner=lambda *_: (_ for _ in ()).throw(
            AssertionError("completed Decision must not rerun Hermes")
        ),
    )
    assert duplicate["knowledge_candidate_id"] == result["knowledge_candidate_id"]
    assert conn.execute("SELECT count(*) FROM knowledge_entries").fetchone()[0] == 1


def test_hermes_review_allows_one_schema_correction_then_fails_closed(conn, config):
    cfg = active_config(config)
    _, job_id = make_job(conn, cfg)
    prepare_codex_review(conn, cfg, job_id=job_id, runner=remote_runner())
    calls = []

    def invalid_runner(argv, prompt, timeout):
        calls.append(prompt)
        return ExecutionResult(argv, 0, "not-json", "")

    with pytest.raises(ReviewError, match="strict JSON"):
        run_hermes_review(
            conn,
            cfg,
            job_id=job_id,
            remote_runner=remote_runner(),
            hermes_runner=invalid_runner,
        )
    assert len(calls) == 2
    assert "Correct the JSON once" in calls[1]
    assert (
        conn.execute(
            "SELECT status FROM codex_reviews WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        == "model_failed"
    )
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_verified_board_request_creates_one_telegram_gate_without_calling_model(
    conn, config, serial_receipt=None
):
    cfg = active_config(config, board=True)
    # make_job allocates the final Case ID, so build the request immediately after
    # observing the deterministic first ID in this empty test database.
    expected_case_id = datetime.now(UTC).strftime("K3-%Y%m%d-0001")
    request = {
        "type": "board",
        "session_id": expected_case_id + "-board-1",
        "estimated_minutes": 35,
        "purpose": "RAM boot and fresh serial validation",
    }
    case_id, job_id = make_job(conn, cfg, requested_actions=[request])
    assert case_id == expected_case_id
    calls = []

    def must_not_run(argv, prompt, timeout):
        calls.append(prompt)
        return ExecutionResult(argv, 1, "", "must not run")

    result = run_hermes_review(
        conn,
        cfg,
        job_id=job_id,
        remote_runner=remote_runner(),
        hermes_runner=must_not_run,
    )
    assert result["followup"]["gate"] == "board"
    assert calls == []
    assert (
        conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()[
            0
        ]
        == "waiting_board"
    )
    approval = conn.execute(
        "SELECT approval_type,status,session_id FROM approvals WHERE case_id=?",
        (case_id,),
    ).fetchone()
    assert tuple(approval) == ("board1_lease", "requested", request["session_id"])
    outbox = conn.execute(
        "SELECT channel,payload_json FROM outbox WHERE case_id=?", (case_id,)
    ).fetchone()
    assert outbox["channel"] == "telegram"
    assert "不再逐条询问" in json.loads(outbox["payload_json"])["text"]
    approved = execute_control(
        conn,
        cfg,
        ControlMessage(
            "owner-user",
            "owner-chat",
            "tg-board-approve",
            f"approve board {result['followup']['approval_id']} {result['followup']['digest']}",
        ),
    )
    assert approved["continuation"]["created"] is True
    continuation = conn.execute(
        "SELECT context_json,state FROM jobs WHERE job_id=?",
        (approved["continuation"]["job_id"],),
    ).fetchone()
    assert continuation["state"] == "queued"
    assert (
        json.loads(continuation["context_json"])["board_session_id"]
        == request["session_id"]
    )
    assert (
        conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()[
            0
        ]
        == "board_testing"
    )
    board = BoardExecutor(
        cfg,
        runner=lambda argv, cwd, timeout: ExecutionResult(argv, 0, "fresh marker", ""),
    )
    board.execute(
        conn,
        case_id=case_id,
        session_id=request["session_id"],
        action={"type": "ram_boot", "uboot_only": True, "timeout": 60},
    )
    if serial_receipt is not None:
        serial_board = BoardExecutor(cfg, runner=lambda argv, cwd, timeout:
                                     ExecutionResult(argv, 0, serial_receipt, ''))
        serial_board.execute(conn, case_id=case_id, session_id=request['session_id'],
                             action={'type': 'serial_wait', 'regex': 'U-Boot', 'timeout': 10})
    board.close_session(
        conn,
        case_id=case_id,
        session_id=request["session_id"],
    )
    continuation_workdir = Path(
        conn.execute(
            "SELECT workdir FROM jobs WHERE job_id=?",
            (approved["continuation"]["job_id"],),
        ).fetchone()[0]
    )
    (continuation_workdir / "codex-final.md").write_text(
        result_text(case_id), encoding="utf-8"
    )
    conn.execute(
        "UPDATE jobs SET state='succeeded',output_digest='board-agent-output' WHERE job_id=?",
        (approved["continuation"]["job_id"],),
    )
    current = conn.execute(
        "SELECT version FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    transition_case(
        conn,
        case_id=case_id,
        after="investigating",
        actor_type="system",
        actor_id="worker",
        reason="board closed",
        expected_version=current["version"],
    )
    record_codex_result(conn, job_id=approved["continuation"]["job_id"])
    board_review = prepare_codex_review(
        conn,
        cfg,
        job_id=approved["continuation"]["job_id"],
        runner=remote_runner(),
    )
    assert board_review["status"] == "verified"
    # Only Git and trusted board receipts count; the model's build log does not.
    assert len(board_review["evidence_ids"]) == (5 if serial_receipt is not None else 4)
    assert (
        sum(check["kind"] == "board_cleanup" for check in board_review["checks"]) == 2
    )


def test_completion_handler_fails_closed_and_notifies_owner(conn, config):
    cfg = active_config(config)
    case_id, job_id = make_job(conn, cfg)
    result = handle_codex_completion(
        conn,
        cfg,
        job_id=job_id,
        remote_runner=remote_runner("c" * 40),
        hermes_runner=lambda argv, prompt, timeout: (_ for _ in ()).throw(
            AssertionError("model must not run after rejected evidence")
        ),
    )
    assert result["ok"] is False
    assert "HEAD changed" in result["error"]
    outbox = conn.execute(
        "SELECT action_type,payload_json FROM outbox WHERE case_id=?", (case_id,)
    ).fetchone()
    assert outbox["action_type"] == "review_failed"
    assert "Codex 原文未外发" in json.loads(outbox["payload_json"])["text"]


def _approved_push_job(conn, cfg):
    expected_case_id = datetime.now(UTC).strftime("K3-%Y%m%d-0001")
    worktree = f"/data/home2/operator/WorkSpace/k3-ai-worktrees/{expected_case_id}/u-boot"
    request = {
        "type": "wip_push",
        "repo": "u-boot",
        "worktree": worktree,
        "destination": "refs/for/main%wip",
        "commits": [HEAD],
        "command": ["git", "push", "origin", "HEAD:refs/for/main%wip"],
    }
    case_id, origin_job_id = make_job(conn, cfg, requested_actions=[request])
    gated = run_hermes_review(
        conn,
        cfg,
        job_id=origin_job_id,
        remote_runner=remote_runner(),
        hermes_runner=lambda *_: (_ for _ in ()).throw(
            AssertionError("model must not run before push approval")
        ),
    )
    approved = execute_control(
        conn,
        cfg,
        ControlMessage(
            "owner-user",
            "owner-chat",
            "tg-push-approve",
            (
                f"approve push {gated['followup']['approval_id']} "
                f"{gated['followup']['digest']}"
            ),
        ),
    )
    push_job_id = approved["continuation"]["job_id"]
    claimed = claim_jobs(conn, "test-push-worker", limit=1)
    assert [job["job_id"] for job in claimed] == [push_job_id]
    return case_id, origin_job_id, push_job_id, gated["followup"]["approval_id"]


def test_approved_push_is_single_attempt_verified_and_finally_reviewed(conn, config):
    cfg = active_config(config, wip_push=True)
    case_id, origin_job_id, push_job_id, approval_id = _approved_push_job(conn, cfg)
    calls = []

    def push_runner(argv, cwd, timeout):
        calls.append(argv)
        if "push -o wip" in argv[-1]:
            return ExecutionResult(argv, 0, "remote: New Changes: 123", "")
        return remote_runner()(argv, cwd, timeout)

    def verifier(action, result):
        return {
            "change": "123",
            "revision": HEAD,
            "patch_set": 7,
            "wip": True,
            "changes": [
                {
                    "change": "123",
                    "revision": HEAD,
                    "patch_set": 7,
                    "wip": True,
                    "project": "uboot/uboot",
                    "branch": "main",
                }
            ],
            "errors": [],
            "project": "uboot/uboot",
            "branch": "main",
        }

    def final_hermes(argv, prompt, timeout):
        review = conn.execute(
            "SELECT review_id,evidence_ids_json FROM codex_reviews WHERE job_id=?",
            (origin_job_id,),
        ).fetchone()
        case = conn.execute(
            "SELECT version FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        decision = {
            "decision_id": f"hermes-review-{review['review_id']}",
            "case_id": case_id,
            "expected_case_version": case["version"],
            "intent": "reply",
            "confidence": 0.92,
            "evidence_ids": json.loads(review["evidence_ids_json"]),
            "reply_draft": "[AI 自动回复]修复已完成验证，并以 WIP Patch Set 7 推送。",
            "proposed_actions": [
                {
                    "type": "feishu_reply",
                    "source_event_pk": conn.execute(
                        "SELECT event_pk FROM inbound_events"
                    ).fetchone()[0],
                    "source_message_id": "om_review",
                }
            ],
            "facts": ["approved revision is the current Gerrit WIP Patch Set 7"],
            "inferences": [],
            "unknowns": [],
        }
        return ExecutionResult(argv, 0, json.dumps(decision), "")

    result = run_wip_push_job(
        conn,
        cfg,
        job_id=push_job_id,
        runner=push_runner,
        verifier=verifier,
        remote_runner=remote_runner(),
        hermes_runner=final_hermes,
    )
    assert result["review"]["ok"] is True
    assert len(calls) == 6
    job = conn.execute(
        "SELECT state,max_attempts,attempt_no FROM jobs WHERE job_id=?", (push_job_id,)
    ).fetchone()
    assert tuple(job) == ("succeeded", 1, 1)
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()[0]
        == "consumed"
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM case_sources WHERE case_id=? AND source_type='gerrit_wip'",
            (case_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()[
            0
        ]
        == "answering"
    )
    reply = conn.execute(
        "SELECT payload_json FROM outbox WHERE case_id=? AND channel='feishu_im'",
        (case_id,),
    ).fetchone()
    assert "WIP Patch Set 7" in json.loads(reply["payload_json"])["text"]


def test_uncertain_push_is_terminal_alerted_and_never_retried(conn, config):
    cfg = active_config(config, wip_push=True)
    case_id, _, push_job_id, approval_id = _approved_push_job(conn, cfg)

    def runner(argv, cwd, timeout):
        if "push -o wip" in argv[-1]:
            return ExecutionResult(argv, 0, "remote accepted", "")
        return remote_runner()(argv, cwd, timeout)

    with pytest.raises(ExecutorError, match="verification is incomplete") as caught:
        run_wip_push_job(
            conn,
            cfg,
            job_id=push_job_id,
            runner=runner,
            verifier=lambda action, result: {
                "change": "123",
                "revision": HEAD,
                "patch_set": None,
                "wip": False,
                "errors": ["Gerrit read-back timed out"],
            },
        )
    failure = fail_wip_push_job(conn, cfg, job_id=push_job_id, error=caught.value)
    assert failure["side_effect"] == "uncertain_or_completed"
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()[0]
        == "consumed"
    )
    assert (
        conn.execute(
            "SELECT state FROM jobs WHERE job_id=?", (push_job_id,)
        ).fetchone()[0]
        == "failed"
    )
    assert (
        conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()[
            0
        ]
        == "error"
    )
    assert (
        conn.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0]
        == 0
    )
    alert = conn.execute(
        "SELECT payload_json FROM outbox WHERE action_type='wip_push_failed'"
    ).fetchone()
    assert "不会自动重试" in json.loads(alert["payload_json"])["text"]


def test_preflight_failure_revokes_unconsumed_approval_and_requires_new_preview(conn, config):
    from k3_support.approvals import expiry_after, request_approval

    cfg = active_config(config, wip_push=True)
    case_id, _, job_id, approval_id = _approved_push_job(conn, cfg)
    result = fail_wip_push_job(conn, cfg, job_id=job_id, error=ExecutorError("remote changed before dispatch"))
    assert result["side_effect"] == "not_started"
    old = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    assert old["status"] == "revoked" and old["consumed_at"] is None
    new_id, _, created = request_approval(
        conn, case_id=case_id, approval_type="wip_push",
        action=json.loads(old["requested_action_json"]), expires_at=expiry_after(30),
    )
    assert created and new_id != approval_id
    assert conn.execute("SELECT status FROM approvals WHERE approval_id=?", (new_id,)).fetchone()[0] == "requested"


def test_late_push_failure_preserves_new_round_activity_and_human_instruction(conn, config):
    cfg = active_config(config, wip_push=True)
    case_id, _, job_id, _ = _approved_push_job(conn, cfg)
    conn.execute(
        "UPDATE cases SET state='takeover',active_job_id='new-round-job',next_action='operator instruction' WHERE case_id=?",
        (case_id,),
    )
    fail_wip_push_job(conn, cfg, job_id=job_id, error=ExecutorError("late failure"))
    row = conn.execute("SELECT state,active_job_id,next_action FROM cases WHERE case_id=?", (case_id,)).fetchone()
    assert tuple(row) == ("takeover", "new-round-job", "operator instruction")
