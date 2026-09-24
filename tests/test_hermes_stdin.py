"""Real pipe/re-exec tests using a synthetic Hermes module, no provider access."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from k3_support import hermes_stdin


@pytest.mark.parametrize('abort', [False, True])
def test_reasoning_display_is_disabled_and_restored(abort):
    from types import SimpleNamespace

    class CLI:
        def _current_reasoning_callback(self):
            return print

    original = CLI._current_reasoning_callback
    try:
        with hermes_stdin.without_reasoning_display(SimpleNamespace(HermesCLI=CLI)):
            assert CLI()._current_reasoning_callback() is None
            if abort:
                raise SystemExit(2)
    except SystemExit:
        assert abort
    assert CLI._current_reasoning_callback is original
    assert CLI()._current_reasoning_callback() is print


@pytest.mark.parametrize(('raw', 'expected'), [
    ('   ', 'empty_json_output'),
    ('{}\nprivate trailing text', 'extra_json_output'),
    ('private leading text\n{}', 'invalid_json_output'),
])
def test_json_failure_classification_never_exposes_output(raw, expected):
    with pytest.raises(json.JSONDecodeError) as caught:
        json.loads(raw)
    assert hermes_stdin.failure_code(caught.value) == expected


@pytest.fixture
def bridge(tmp_path):
    root = tmp_path / "hermes"
    root.mkdir()
    cli = root / "cli.py"
    cli.write_text("""import json, os
class HermesCLI:
    def _current_reasoning_callback(self):
        return print
def main(**kwargs):
    import run_agent, model_tools
    assert HermesCLI()._current_reasoning_callback() is None
    assert run_agent.get_tool_definitions() == []
    assert model_tools.get_tool_definitions() == []
    secret = kwargs["query"]
    assert secret
    argv = open("/proc/self/cmdline", "rb").read()
    assert secret.encode() not in argv
    assert secret not in str(dict(os.environ))
    assert kwargs["model"] == "fixture-model"
    assert kwargs["provider"] == "fixture-provider"
    print(json.dumps({"knowledge_id": "knw_fixture", "confidence": 0.9}))
""")
    agent = root / "run_agent.py"
    agent.write_text("def get_tool_definitions(*args, **kwargs): return ['unsafe']\n"
                     "def handle_function_call(*args, **kwargs): raise AssertionError('unsafe dispatch')\n")
    (root / "model_tools.py").write_text(agent.read_text())
    config = tmp_path / "bridge.json"
    value = {
        "schema_version": 1,
        "python": sys.executable,
        "root": str(root),
        "model": "fixture-model",
        "provider": "fixture-provider",
        "cli_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
        "run_agent_sha256": hashlib.sha256(agent.read_bytes()).hexdigest(),
    }
    config.write_text(json.dumps(value))
    config.chmod(0o600)
    env = {
        "HOME": str(tmp_path),
        "PATH": os.defpath,
        "K3_SUPPORT_HERMES_BRIDGE_CONFIG": str(config),
    }

    def run(payload, flag="--support-json-stdin"):
        return subprocess.run(
            [sys.executable, "-I", str(Path(hermes_stdin.__file__).resolve()), flag],
            input=payload,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
            env=env,
        )

    return run, config, cli


def test_real_reexec_keeps_prompt_off_os_argv_and_environment(bridge):
    run, _, _ = bridge
    result = run(
        json.dumps(
            {
                "protocol": 1,
                "prompt": "private-colleague-question-秘密",
                "reasoning": "medium",
            }
        )
    )
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == {
        "knowledge_id": "knw_fixture",
        "confidence": 0.9,
    }


def test_check_does_not_load_model_or_need_prompt(bridge):
    run, _, _ = bridge
    result = run("", "--check")
    assert result.returncode == 0
    assert json.loads(result.stdout)["inference_verified"] is False
    assert json.loads(result.stdout)["supported_protocols"] == [1]
    assert json.loads(result.stdout)["checked_source_count"] == 2


@pytest.mark.parametrize("changed_source", sorted(hermes_stdin.RECEIPT_SOURCES))
def test_receipt_manifest_requires_all_observation_sources(bridge, changed_source):
    run, config, _ = bridge
    value = json.loads(config.read_text())
    value["schema_version"] = 2
    value["receipt_sources"] = {}
    for name in hermes_stdin.RECEIPT_SOURCES:
        source = Path(value["root"]) / name
        source.parent.mkdir(exist_ok=True)
        source.write_text("# fixture observation module\n")
        value["receipt_sources"][name] = hashlib.sha256(source.read_bytes()).hexdigest()
    config.write_text(json.dumps(value))
    checked = run("", "--check")
    assert checked.returncode == 0
    assert json.loads(checked.stdout)["supported_protocols"] == [1, 2]
    assert json.loads(checked.stdout)["checked_source_count"] == 2 + len(hermes_stdin.RECEIPT_SOURCES)
    before = config.read_bytes()
    generated = hermes_stdin.manifest_template(root=value["root"], python=value["python"],
                                              model=value["model"], provider=value["provider"])
    assert generated == value
    bound = hermes_stdin.manifest_template(root=value["root"], python=value["python"],
        model=value["model"], provider=value["provider"], runtime_provider="custom", endpoint_sha256="a" * 64)
    assert bound["runtime_provider"] == "custom" and bound["endpoint_sha256"] == "a" * 64
    for incomplete in ({"runtime_provider": "custom"}, {"endpoint_sha256": "a" * 64}):
        with pytest.raises(hermes_stdin.BridgeError):
            hermes_stdin.manifest_template(root=value["root"], python=value["python"],
                model=value["model"], provider=value["provider"], **incomplete)
    assert config.read_bytes() == before
    with pytest.raises(hermes_stdin.BridgeError):
        hermes_stdin.manifest_template(root="relative", python=value["python"],
                                      model=value["model"], provider=value["provider"])
    target = Path(value["root"]) / changed_source
    target.write_text("# changed producer\n")
    assert run("", "--check").returncode == 2
    del value["receipt_sources"][changed_source]
    config.write_text(json.dumps(value))
    assert run("", "--check").returncode == 2


@pytest.mark.parametrize("mode", ["valid", "missing", "duplicate", "wrong_model", "truncated"])
def test_real_receipt_reexec_pipeline(bridge, mode):
    from k3_support.ids import digest

    run, config, cli = bridge
    value = json.loads(config.read_text())
    root = Path(value["root"])
    value["schema_version"] = 2
    value["receipt_sources"] = {}
    for name in hermes_stdin.RECEIPT_SOURCES:
        source = root / name
        source.parent.mkdir(exist_ok=True)
        if not source.exists():
            source.write_text("# isolated fixture module\n")
    (root / "hermes_cli/lifecycle.py").write_text(
        "def has_hook(name): return False\ndef invoke_hook(name, **event): return []\n")
    cli.write_text('''import json
from hermes_cli import lifecycle
def main(**kwargs):
    assert kwargs['query'] == 'private-receipt-question'
    assert kwargs['model'] == 'fixture-model'
    assert lifecycle.has_hook('post_api_request')
    mode = ''' + repr(mode) + '''
    if mode != 'missing':
        for number in range(2 if mode == 'duplicate' else 1):
            lifecycle.invoke_hook('post_api_request', api_request_id='fixture-request',
                session_id='fixture-session', model=kwargs['model'], provider=kwargs['provider'],
                response_model='different' if mode == 'wrong_model' else kwargs['model'],
                api_call_count=1, assistant_tool_call_count=0,
                finish_reason='length' if mode == 'truncated' else 'stop',
                response={'private': kwargs['query']}, base_url='private-endpoint')
    print(json.dumps({'knowledge_id':'fixture', 'confidence':0.95}))
''')
    value["cli_sha256"] = hashlib.sha256(cli.read_bytes()).hexdigest()
    for name in hermes_stdin.RECEIPT_SOURCES:
        value["receipt_sources"][name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    config.write_text(json.dumps(value))
    request = {"protocol": 2, "prompt": "private-receipt-question", "reasoning": "low",
               "expected_manifest_digest": digest(value)}
    result = run(json.dumps(request))
    assert "private-receipt-question" not in result.stdout + result.stderr
    assert "private-endpoint" not in result.stdout + result.stderr
    if mode == "valid":
        assert result.returncode == 0
        envelope = json.loads(result.stdout)
        assert envelope["request_digest"] == digest(request)
        assert envelope["manifest_digest"] == digest(value)
        assert envelope["receipt"]["response_model"] == "fixture-model"
        assert envelope["result"]["knowledge_id"] == "fixture"
    else:
        assert result.returncode == 2 and result.stdout == ""


def test_real_bridge_rejects_changed_manifest_identity(bridge):
    from k3_support.ids import digest

    run, config, _ = bridge
    value = json.loads(config.read_text())
    request = {"protocol": 1, "prompt": "private-question", "reasoning": "low",
               "expected_manifest_digest": digest(value)}
    assert run(json.dumps(request)).returncode == 0
    value["model"] = "unexpected-model"
    config.write_text(json.dumps(value))
    result = run(json.dumps(request))
    assert result.returncode == 2
    assert result.stdout == ""
    assert "private-question" not in result.stderr


def test_identity_mismatch_never_calls_entry():
    def forbidden(**kwargs):
        pytest.fail("mismatched identity reached the model")

    with pytest.raises(hermes_stdin.BridgeError, match="identity changed"):
        hermes_stdin.execute({}, {"protocol": 1, "prompt": "private", "reasoning": "low",
                                  "expected_manifest_digest": "0" * 64}, entry=forbidden)


@pytest.mark.parametrize(
    "failure", ["source_change", "public_manifest", "malformed", "too_large"]
)
def test_bridge_failure_is_generic_and_never_echoes_input(bridge, failure):
    run, config, cli = bridge
    payload = json.dumps(
        {"protocol": 1, "prompt": "sensitive-body", "reasoning": "medium"}
    )
    if failure == "source_change":
        cli.write_text("raise RuntimeError('must not import changed source')")
    elif failure == "public_manifest":
        config.chmod(0o644)
    elif failure == "malformed":
        payload = "sensitive-body"
    else:
        payload = "sensitive-body" * 200000
    result = run(payload)
    assert result.returncode == 2 and result.stdout == ""
    assert "sensitive-body" not in result.stderr and "Traceback" not in result.stderr


def test_semantic_timeout_has_no_argv_fallback(monkeypatch):
    from k3_support.semantic import _hermes_json

    calls = []

    def fail(argv, **kwargs):
        calls.append(argv)
        assert kwargs["input"] and "private" not in str(argv)
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr("k3_support.semantic.subprocess.run", fail)
    assert _hermes_json("private", reasoning="low", timeout=1) is None
    assert len(calls) == 1
