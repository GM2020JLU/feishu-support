#!/usr/bin/env python3
"""Synthetic Linux sandbox acceptance; no live config, model or database."""

import json
import sysconfig
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from k3_support import replay_scenario
from k3_support.db import connect, migrate
from k3_support.replay_history import run_snapshot_inference, run_snapshot_replay
from k3_support.replay_sandbox import run_sandbox


def verify():
    template = Path(__file__).resolve().parents[1] / "config/config.example.yaml"
    config = yaml.safe_load(template.read_text())
    config["mode"] = "active"
    config["identity"]["feishu_owner_open_id"] = "ou_replay_owner"
    config["identity"]["telegram_control_user_id"] = "replay-owner"
    config["identity"]["telegram_control_chat_id"] = "replay-control"
    config["scope"]["technical_chat_ids"] = ["oc_replay"]
    config["scope"]["auto_reply_chat_ids"] = ["oc_replay"]
    proposal = {
        "route": "research",
        "confidence": 0.96,
        "issue_type": "investigation",
        "severity": "P3",
        "domain": "bootloader",
        "repository_hints": [],
        "reason_codes": ["technical_investigation"],
        "clarification_question": None,
        "fallback_route": None,
        "requires_owner_judgment": False,
        "conversation_relation": "standalone",
    }

    def message(number):
        payload = {"content": "Synthetic Pico boot investigation", "chat_type": "group"}
        payload.update(
            {"mentions": [{"id": "ou_replay_owner"}]}
            if number == 1
            else {"parent_id": "om_replay_1"}
        )
        return {
            "source": "feishu_user_poll",
            "identity": "user",
            "external_id": f"om_replay_{number}",
            "payload": payload,
            "occurred_at": datetime.now(UTC).isoformat(),
            "sender_id": "ou_replay_peer",
            "chat_id": "oc_replay",
        }

    result = replay_scenario.run_isolated_scenario(
        {
            "config": config,
            "steps": [
                {"event": message(1), "proposal": proposal},
                {"case_step": 0, "action": "claim"},
                {"event": message(2), "proposal": proposal},
            ],
        }
    )
    if not (
        result["steps"][2]["result"].get("ignored")
        and result["steps"][2]["intentions"] == {"outbox": [], "jobs": []}
    ):
        raise ValueError("human takeover acceptance failed")
    supervisor = replay_scenario.run_isolated_scenario(
        {
            "config": config,
            "profiles": [
                {
                    "requester_id": "ou_replay_peer",
                    "relationship": "supervisor",
                    "function_role": "management",
                }
            ],
            "steps": [
                {
                    "event": message(1),
                    "proposal": proposal
                    | {
                        "route": "clarify",
                        "reason_codes": ["missing_logs"],
                        "clarification_question": "Please send every log?",
                        "fallback_route": "research",
                    },
                }
            ],
        }
    )
    if supervisor["steps"][0]["result"]["route"]["route"] == "clarify":
        raise ValueError("supervisor strategy acceptance failed")
    research = replay_scenario.run_isolated_scenario(
        {
            "config": config | {"features": config["features"] | {"auto_faq": True}},
            "steps": [
                {"event": message(1), "proposal": proposal},
                {
                    "research_for_step": 0,
                    "documents": [
                        {
                            "title": "Synthetic guide",
                            "url": "https://example.com/pico",
                            "content": "Synthetic documentation only.",
                        }
                    ],
                    "selection": {
                        "document_urls": ["https://example.com/pico"],
                        "confidence": 0.99,
                    },
                },
            ],
        }
    )
    if not research["steps"][1]["research"]["retrieval"]["followup_eligible"]:
        raise ValueError("fixture retrieval acceptance failed")
    debug = replay_scenario.run_isolated_scenario(
        {
            "config": config | {"features": config["features"] | {"codex": True}},
            "steps": [
                {
                    "event": message(1),
                    "proposal": proposal
                    | {
                        "route": "codex_debug",
                        "issue_type": "bug",
                        "repository_hints": ["u-boot"],
                    },
                },
                {"research_for_step": 0, "documents": [], "selection": None},
            ],
        }
    )
    debug_research = debug["steps"][1]["research"]
    if (
        debug_research["child_job"] != {"job_type": "codex", "state": "queued"}
        or debug_research["completion"]["parent_job_id"]
        != debug_research["retrieval"]["job_id"]
    ):
        raise ValueError("Codex queue boundary acceptance failed")
    common = {
        "package": Path(replay_scenario.__file__).parent,
        "site_packages": Path(sysconfig.get_paths()["purelib"]),
        "request": {},
    }
    try:
        run_sandbox(**common, program="import time; time.sleep(60)", timeout=0.5)
    except TimeoutError:
        pass
    else:
        raise ValueError("deadline acceptance failed")
    try:
        run_sandbox(**common, program='print("x"*10000)', output_limit=100)
    except ValueError as exc:
        if "output limit" not in str(exc):
            raise
    else:
        raise ValueError("output bound acceptance failed")
    expiry = replay_scenario.run_isolated_scenario(
        {
            "config": config,
            "steps": [
                {"global_mode": "auto_60"},
                {"mode_elapsed_minutes": 59},
                {"mode_elapsed_minutes": 60},
            ],
        }
    )
    if [step["global_control"]["mode"] for step in expiry["steps"]] != [
        "auto_60",
        "auto_60",
        "collaborate",
    ]:
        raise RuntimeError("isolated automatic-mode expiry failed")
    if (
        expiry["steps"][-1]["global_control"]["clock_scope"]
        != "global_mode_only_not_worker_leases"
    ):
        raise RuntimeError("isolated expiry scope is missing")
    takeover_expiry = replay_scenario.run_isolated_scenario(
        {
            "config": config,
            "steps": [
                {"event": message(1), "proposal": proposal},
                {"case_step": 0, "action": "claim"},
                {"global_mode": "auto_60"},
                {"mode_elapsed_minutes": 60},
                {"event": message(2), "proposal": proposal},
            ],
        }
    )
    followup = takeover_expiry["steps"][-1]
    if not followup["result"].get("ignored") or followup["intentions"] != {
        "jobs": [],
        "outbox": [],
    }:
        raise RuntimeError("human ownership was lost across automatic-mode expiry")
    with tempfile.TemporaryDirectory(prefix="codex-replay-snapshot-") as scratch:
        database = Path(scratch) / "synthetic.db"
        source = connect(database)
        try:
            migrate(source)
            before = source.serialize()
            snapshot = run_snapshot_replay(
                database,
                {
                    "config": config,
                    "event": message(1),
                    "proposal": proposal,
                },
            )
            if snapshot["report"]["result"]["route"]["route"] != "research":
                raise RuntimeError("snapshot route acceptance failed")
            if source.serialize() != before:
                raise RuntimeError("snapshot source changed")
            observed_inputs = []

            def synthetic_router(value):
                observed_inputs.append(value)
                return proposal

            inferred = run_snapshot_inference(
                database,
                {
                    "config": config,
                    "event": message(1),
                },
                router=synthetic_router,
            )
            if (
                len(observed_inputs) != 1
                or inferred["report"]["result"]["route"]["route"] != "research"
            ):
                raise RuntimeError("two-phase routing inference acceptance failed")
            if source.serialize() != before or inferred["model_invoked"] is not None:
                raise RuntimeError(
                    "inference source or provider evidence boundary failed"
                )
        finally:
            source.close()
    return {
        "ok": True,
        "checks": [
            "sealed_snapshot_transfer",
            "two_phase_routing_with_synthetic_callback",
            "isolated_inbound",
            "human_takeover_followup",
            "supervisor_no_auto_clarification",
            "fixture_retrieval",
            "codex_queued_not_executed",
            "execution_deadline",
            "output_limit",
            "isolated_auto60_expiry",
            "human_takeover_survives_auto60_expiry",
        ],
        "model_invoked": False,
        "live_data_used": False,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(verify()))
    except Exception as exc:  # noqa: BLE001 -- sanitize the CLI error boundary
        # Never dump configuration, input messages or child stderr on failure.
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
