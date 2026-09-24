import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from k3_support.ids import digest


def test_timeout_retains_only_allowlisted_phases(monkeypatch):
    import subprocess

    spec = importlib.util.spec_from_file_location('canary', Path(__file__).parents[1] / 'scripts/verify-hermes-bridge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.hermes_stdin, 'validate_manifest', lambda value: value)
    calls = []

    def run(argv, **kwargs):
        assert '--diagnostics' in argv
        calls.append(kwargs)
        raw = '\n'.join([json.dumps({'bridge_phase': 'cli_started', 'elapsed_ms': 12}),
                         json.dumps({'bridge_phase': ['PRIVATE'], 'elapsed_ms': 2}),
                         json.dumps({'error': 'bridge_failure', 'failure_code': ['PRIVATE']}),
                         'PRIVATE ERROR AND ENDPOINT'])
        raise subprocess.TimeoutExpired('PRIVATE COMMAND', 1, stderr=raw.encode())

    monkeypatch.setattr(module.subprocess, 'run', run)
    result = module.verify({'schema_version': 2, 'model': 'fixture', 'provider': 'fixture'})
    assert result['failure_code'] == 'timeout'
    assert result['phases'] == [{'bridge_phase': 'cli_started', 'elapsed_ms': 12}]
    assert 'PRIVATE' not in str(result)
    assert len(calls) == 1 and not Path(calls[0]['cwd']).exists()


@pytest.mark.parametrize("failure", [None, "request", "model", "process"])
def test_canary_uses_private_stdin_and_never_retries(monkeypatch, failure):
    spec = importlib.util.spec_from_file_location("canary", Path(__file__).parents[1] / "scripts/verify-hermes-bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.hermes_stdin, "validate_manifest", lambda value: value)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "unrelated-task")
    identity = {"schema_version": 2, "model": "fixture", "provider": "fixture-provider"}
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        request = json.loads(kwargs["input"])
        assert request["prompt"] not in str(argv) + str(kwargs["env"])
        assert "HERMES_KANBAN_TASK" not in kwargs["env"]
        manifest = Path(kwargs["env"]["K3_SUPPORT_HERMES_BRIDGE_CONFIG"])
        assert manifest.stat().st_mode & 0o777 == 0o600
        assert json.loads(manifest.read_text()) == identity
        receipt = {"model": "fixture", "response_model": "fixture", "provider": "fixture-provider", "finish_reason": "stop"}
        if failure == "model": receipt["response_model"] = "wrong"
        envelope = {"protocol": 2, "result": {"canary": "ok"}, "receipt": receipt,
                    "manifest_digest": digest(identity), "request_digest": "wrong" if failure == "request" else digest(request)}
        return SimpleNamespace(returncode=2 if failure == "process" else 0, stdout=json.dumps(envelope), stderr="PRIVATE ERROR")

    monkeypatch.setattr(module.subprocess, "run", run)
    result = module.verify(identity)
    assert result["ok"] is (failure is None)
    assert len(calls) == 1 and not Path(calls[0]["cwd"]).exists()
    assert "PRIVATE ERROR" not in str(result)
