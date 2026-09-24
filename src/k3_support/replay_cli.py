"""Installed, sandbox-only fixture replay. Never loads the owner's live config."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from .replay_history import run_snapshot_inference, run_snapshot_replay
from .replay_scenario import run_isolated_scenario
from .semantic import hermes_message_router


def pipeline_summary(value):
    pipeline = value['result']
    turns = pipeline.get('turns', [pipeline])
    reports = []
    for number, turn in enumerate(turns, 1):
        steps = [turn['inbound']]
        if turn['research'] is not None:
            steps.append({'research': turn['research']})
        report = {'turn': number, **summary({'steps': steps, 'scope': value['scope'],
            'knowledge_scope': 'current_snapshot_and_supplied_document_fixtures',
            'profile_scope': 'current_snapshot_with_explicit_assumptions'})}
        report.update(model_invoked=None, external_consumers=False,
                      model_callback_invoked=any(call.get('turn', 0) == number - 1
                                                 for call in value['calls']))
        reports.append(report)
    return {'ok': True, 'turns': reports, 'scope': value['scope'],
            'calls': value['calls'], 'assumptions': value['assumptions'],
            'content_included': False, 'external_consumers': False,
            'model_invoked': None, 'model_callback_invoked': bool(value['calls']),
            'provider_verification': value['provider_verification']}


def summary(result: dict) -> dict:
    if "report" in result:
        safe = summary(
            {
                "steps": [result["report"]],
                "scope": result["scope"],
                "knowledge_scope": "current_snapshot",
                "profile_scope": "current_snapshot",
            }
        )
        safe["external_consumers"] = False
        for key in (
            "model_invoked",
            "model_callback_invoked",
            "inference_scope",
            "provider_verification",
            "assumptions",
            "clock_scope",
            "business_window",
        ):
            if key in result:
                safe[key] = result[key]
        return safe
    steps = []
    for item in result["steps"]:
        if "global_control" in item:
            steps.append(
                {"kind": "global_control", "mode": item["global_control"]["mode"]}
            )
            continue
        if "research" in item:
            completion = item["research"].get("completion") or {}
            steps.append(
                {
                    "kind": "research",
                    "state": completion.get("state"),
                    "retrieval_state": item["research"]["retrieval"].get(
                        "state", "succeeded"
                    ),
                    "codex_queued": bool(completion.get("job_id")),
                    "source_scope": item["research"]["source_scope"],
                }
            )
            continue
        if "communication" in item:
            steps.append(
                {
                    "kind": "communication",
                    "action": item["communication"].get("command"),
                }
            )
            continue
        value = item["result"]
        from .orchestrator import _REASON_LABELS
        proposed_reasons = (value.get('route') or {}).get('reason_codes', [])
        reasons = list(dict.fromkeys(code for code in proposed_reasons
            if isinstance(code, str) and code in _REASON_LABELS)) if isinstance(proposed_reasons, list) else []
        steps.append(
            {
                "kind": "inbound",
                "route": (value.get("route") or {}).get("route"),
                "reason_codes": reasons,
                "reason_labels": [_REASON_LABELS[code] for code in reasons],
                "ignored": bool(value.get("ignored")),
                "blocked_by_mode": value.get("blocked_by_mode"),
                "outbox_intentions": len(item["intentions"]["outbox"]),
                "job_intentions": len(item["intentions"]["jobs"]),
            }
        )
    return {
        "ok": True,
        "steps": steps,
        "model_invoked": False,
        "scope": result["scope"],
        "knowledge_scope": result["knowledge_scope"],
        "profile_scope": result["profile_scope"],
        "content_included": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay stdin JSON in isolation; models require explicit inference flags; never sends."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="Execution deadline in seconds (0 < value <= 300).",
    )
    parser.add_argument(
        "--include-content",
        action="store_true",
        help="Print private message/notification contents; default prints only counts and routes.",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="Explicit source database: copy read-only into isolation. Stdin requires config/event/proposal; not historical time travel.",
    )
    parser.add_argument(
        "--infer-route",
        action="store_true",
        help="Explicitly send routing observations to the configured Hermes bridge; requires --snapshot and config/event only on stdin. May incur provider cost.",
    )
    parser.add_argument(
        "--inference-timeout",
        type=int,
        default=45,
        help="Separate bridge timeout in seconds, 1..300; no retries.",
    )
    parser.add_argument('--infer-pipeline', action='store_true',
        help='Explicit model routing/document selection for event or events plus supplied documents; requires --snapshot. Up to 20 calls, may incur cost.')
    parser.add_argument('--review-clarification', action='store_true',
        help='With --infer-pipeline, explicitly enable clarification review (up to 30 total calls).')
    parser.add_argument('--infer-debug', action='store_true',
        help='Explicitly review captured or simulated Debug execution in a sealed snapshot; at most one model call, may incur cost. Requires --snapshot.')
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 300:
        parser.error("timeout must be finite and in (0, 300]")
    if args.infer_route and not args.snapshot:
        parser.error("--infer-route requires --snapshot")
    if args.infer_pipeline and (not args.snapshot or args.infer_route):
        parser.error('--infer-pipeline requires --snapshot and excludes --infer-route')
    if args.review_clarification and not args.infer_pipeline:
        parser.error('--review-clarification requires --infer-pipeline')
    if args.infer_debug and (not args.snapshot or args.infer_route or args.infer_pipeline or args.review_clarification):
        parser.error('--infer-debug requires --snapshot and excludes other inference modes')
    if not 1 <= args.inference_timeout <= 300:
        parser.error("inference timeout must be 1..300")
    try:
        data = sys.stdin.buffer.read(262145)
        if len(data) > 262144:
            raise ValueError("input exceeds limit")
        request = json.loads(data)
        if args.infer_debug:
            from .replay_debug_snapshot import run
            from .semantic import _hermes_json

            def reviewer(value):
                return json.dumps(_hermes_json(value['prompt'], reasoning='medium',
                                              timeout=args.inference_timeout), allow_nan=False)

            result = run(args.snapshot, request, reviewer=reviewer, timeout=args.timeout)
            lifecycle = result['result']
            captured = lifecycle.get('review') if 'worker' in lifecycle else lifecycle
            decision = captured.get('review') if captured else None
            output = ({'ok': True, 'content_included': True, 'result': result}
                      if args.include_content else {
                          'ok': True, 'scope': result['scope'], 'content_included': False,
                          'completion': (lifecycle.get('completion') or {}).get('state'),
                          'review_ok': decision.get('ok') if decision else None,
                          'model_calls': len(result['calls']), 'model_invoked': result['model_invoked'],
                          'provider_verification': result['provider_verification'],
                          'outbox_intentions': len(lifecycle['outbox_intentions']),
                          'external_consumers': False})
            print(json.dumps(output, ensure_ascii=False, allow_nan=False))
            return 0
        if args.infer_pipeline:
            from .replay_model_pipeline import run
            from .semantic import hermes_research_link_selector, hermes_clarification_reviewer
            result = run(args.snapshot, request, timeout=args.timeout,
                router=lambda value: hermes_message_router(value, timeout=args.inference_timeout),
                selector=lambda value: hermes_research_link_selector(value, timeout=args.inference_timeout),
                clarification_reviewer=(lambda value: hermes_clarification_reviewer(
                    value, timeout=args.inference_timeout)) if args.review_clarification else None)
            output = ({'ok': True, 'content_included': True, 'result': result}
                      if args.include_content else pipeline_summary(result))
            print(json.dumps(output, ensure_ascii=False, allow_nan=False))
            return 0
        result = (
            run_snapshot_inference(
                args.snapshot,
                request,
                timeout=args.timeout,
                router=lambda value: hermes_message_router(
                    value, timeout=args.inference_timeout
                ),
            )
            if args.infer_route
            else (
                run_snapshot_replay(args.snapshot, request, timeout=args.timeout)
                if args.snapshot
                else run_isolated_scenario(request, timeout=args.timeout)
            )
        )
        output = (
            {"ok": True, "content_included": True, "result": result}
            if args.include_content
            else summary(result)
        )
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:  # noqa: BLE001 -- no raw request or subprocess error disclosure
        print(
            json.dumps({"ok": False, "error_type": type(exc).__name__}), file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
