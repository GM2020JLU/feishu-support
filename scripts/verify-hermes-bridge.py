#!/usr/bin/env python3
"""Explicit, single synthetic provider request; no deployment or workflow database."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from k3_support import hermes_stdin
from k3_support.ids import digest


def diagnostics(stderr):
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', errors='replace')
    phases, code = [], 'unclassified_bridge_failure'
    for line in (stderr or '')[:65536].splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        if (set(value) == {'bridge_phase', 'elapsed_ms'} and isinstance(value['bridge_phase'], str) and value['bridge_phase'] in hermes_stdin.PHASES
                and type(value['elapsed_ms']) is int and 0 <= value['elapsed_ms'] <= 3600000 and len(phases) < 32):
            phases.append(value)
        allowed = hermes_stdin.FAILURE_CODES | {'runtime_exit', 'invalid_json_output', 'empty_json_output', 'extra_json_output', 'bridge_validation_failed', 'runtime_failed'}
        if set(value) == {'error', 'failure_code'} and value['error'] == 'bridge_failure' and isinstance(value['failure_code'], str) and value['failure_code'] in allowed:
            code = value['failure_code']
    return phases, code


def verify(identity, *, timeout=60, prompt=None, reasoning='medium'):
    if reasoning not in {'low', 'medium'}:
        raise ValueError('unsupported reasoning effort')
    hermes_stdin.validate_manifest(identity)
    request = {"protocol": 2, "reasoning": reasoning, "expected_manifest_digest": digest(identity),
               "prompt": prompt if prompt is not None else 'Synthetic connectivity test only. Do not call tools or contact anyone. Return only JSON: {"canary":"ok"}'}
    with tempfile.TemporaryDirectory(prefix="codex-hermes-canary-") as directory:
        path = Path(directory) / "manifest.json"
        with path.open("x", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            json.dump(identity, handle)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("HERMES_KANBAN_")}
        environment["K3_SUPPORT_HERMES_BRIDGE_CONFIG"] = str(path)
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(Path(hermes_stdin.__file__).resolve()), "--support-json-stdin", '--diagnostics'],
                input=json.dumps(request), text=True, capture_output=True, timeout=timeout,
                env=environment, cwd=directory, check=False,
            )
        except subprocess.TimeoutExpired as error:
            phases, _ = diagnostics(error.stderr)
            return {'ok': False, 'stage': 'bridge_process', 'failure_code': 'timeout', 'phases': phases}
    phases, code = diagnostics(result.stderr)
    if result.returncode != 0:
        return {"ok": False, "stage": "bridge_process", "returncode": result.returncode, 'failure_code': code, 'phases': phases}
    envelope = json.loads(result.stdout)
    receipt = envelope.get("receipt", {})
    valid = (
        envelope.get("protocol") == 2
        and (isinstance(envelope.get("result"), dict) if prompt is not None else envelope.get("result") == {"canary": "ok"})
        and envelope.get("manifest_digest") == digest(identity)
        and envelope.get("request_digest") == digest(request)
        and receipt.get("model") == identity["model"]
        and receipt.get("response_model") == identity["model"]
        and receipt.get("provider") == identity.get("runtime_provider", identity["provider"])
        and receipt.get("finish_reason") == "stop"
        and ("endpoint_sha256" not in identity or (
            receipt.get("endpoint_sha256") == identity["endpoint_sha256"]
            and receipt.get("requested_provider") == identity["provider"]))
    )
    report = {"ok": valid, "stage": "receipt_validation", "manifest_digest": digest(identity),
            'phases': phases,
            "model": receipt.get("response_model"), "provider": receipt.get("provider"),
            "requested_provider": identity["provider"], "billing_verified": False,
            "scope": "one synthetic stdin/re-exec request; not workflow or knowledge quality acceptance"}
    if prompt is not None and valid:
        report['result'] = envelope['result']
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="Previously checked private bridge manifest")
    args = parser.parse_args()
    try:
        report = verify(hermes_stdin.manifest(args.manifest))
    except (Exception, SystemExit) as error:  # noqa: BLE001 - never echo credential-bearing failures
        report = {"ok": False, "error_type": type(error).__name__, "details_redacted": True}
    print(json.dumps(report))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
