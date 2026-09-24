import hashlib
import json
from types import SimpleNamespace

import pytest

from k3_support.hermes_stdin import (
    BridgeError,
    InferenceReceipt,
    execute,
    observe_inference,
    without_model_tools,
)
from k3_support.ids import digest


def event(**changes):
    return dict(api_request_id="request", session_id="session", model="model", provider="provider",
                response_model="model", api_call_count=1, assistant_tool_call_count=0, finish_reason="stop",
                **changes)


@pytest.mark.parametrize("wrong", [None, "endpoint", "provider", "missing"])
def test_named_provider_requires_explicit_endpoint_binding(wrong):
    endpoint = "https://fixture.invalid/v1"
    fingerprint = hashlib.sha256(endpoint.encode()).hexdigest()
    collector = InferenceReceipt("model", "custom:chosen", runtime_provider="custom", endpoint_sha256=fingerprint)
    observed = {**event(), "provider": "custom", "base_url": endpoint + "/"}
    if wrong == "endpoint": observed["base_url"] = "https://other.invalid/v1"
    if wrong == "provider": observed["provider"] = "other"
    if wrong == "missing": observed.pop("base_url")
    collector.observe(**observed)
    if wrong:
        with pytest.raises(BridgeError): collector.finish()
    else:
        receipt = collector.finish()
        assert receipt["provider"] == "custom" and receipt["requested_provider"] == "custom:chosen"
        assert receipt["endpoint_sha256"] == fingerprint
        assert endpoint not in str(receipt)


def test_wrong_endpoint_is_rejected_before_provider_request():
    collector = InferenceReceipt("model", "custom:chosen", runtime_provider="custom", endpoint_sha256="a" * 64)
    lifecycle = SimpleNamespace(has_hook=lambda name: False, invoke_hook=lambda name, **kwargs: [])
    with observe_inference(lifecycle, collector), pytest.raises(SystemExit):
        lifecycle.invoke_hook("pre_api_request", tool_count=0, provider="custom", model="model", base_url="https://wrong.invalid")


@pytest.mark.parametrize('identity', [{}, {'model':'wrong','provider':'provider'},
    {'model':'model','provider':'wrong'}, {'model':'model','provider':'provider'}])
def test_request_identity_checked_without_endpoint_binding(identity):
    calls = []
    lifecycle = SimpleNamespace(has_hook=lambda _:False, invoke_hook=lambda *a, **k:calls.append(1))
    collector = InferenceReceipt('model','provider')
    valid = identity == {'model':'model','provider':'provider'}
    with observe_inference(lifecycle, collector):
        if valid:
            lifecycle.invoke_hook('pre_api_request',tool_count=0,**identity)
        else:
            with pytest.raises(SystemExit) as error:
                lifecycle.invoke_hook('pre_api_request',tool_count=0,**identity)
            assert error.value.reason == 'request_identity_mismatch'
    assert calls == ([1] if valid else [])


@pytest.mark.parametrize('first_completed', [False, True])
def test_second_request_is_aborted_before_original_hook_or_provider(first_completed):
    invoked = []
    lifecycle = SimpleNamespace(has_hook=lambda _:False,
        invoke_hook=lambda name, **kwargs: invoked.append(name))
    original = lifecycle.invoke_hook
    collector = InferenceReceipt('model','provider')
    with observe_inference(lifecycle, collector):
        lifecycle.invoke_hook('pre_api_request', tool_count=0, model='model', provider='provider')
        if first_completed:
            lifecycle.invoke_hook('post_api_request', **event())
        with pytest.raises(SystemExit) as error:
            lifecycle.invoke_hook('pre_api_request', tool_count=0, model='model', provider='provider')
        assert error.value.reason == 'multiple_api_requests'
        assert invoked.count('pre_api_request') == 1
        with pytest.raises(BridgeError):
            collector.finish()
    assert lifecycle.invoke_hook is original


def test_tool_guard_blocks_dispatch_and_restores_exact_functions():
    def original_definitions():
        return ["skill_manage"]

    calls = []
    modules = [SimpleNamespace(get_tool_definitions=original_definitions,
                               handle_function_call=lambda: calls.append(1)) for _ in range(3)]
    original_handlers = [module.handle_function_call for module in modules]
    with without_model_tools(*modules):
        for module in modules:
            assert module.get_tool_definitions() == []
            with pytest.raises(SystemExit):
                module.handle_function_call()
    assert calls == []
    for module, handler in zip(modules, original_handlers, strict=True):
        assert module.get_tool_definitions is original_definitions
        assert module.handle_function_call is handler


def test_unknown_tool_interface_fails_before_partial_patching():
    original = lambda: ["tool"]
    module = SimpleNamespace(get_tool_definitions=original, handle_function_call=lambda: None)
    with pytest.raises(BridgeError), without_model_tools(module, module, SimpleNamespace()):
        pytest.fail("unsupported interface entered")
    assert module.get_tool_definitions is original


@pytest.mark.parametrize("count", [None, True, -1, 1, "0", 0])
def test_final_tool_surface_checked_before_provider_call(count):
    lifecycle = SimpleNamespace(has_hook=lambda name: False, invoke_hook=lambda name, **kwargs: [])
    original = lifecycle.has_hook, lifecycle.invoke_hook
    collector = InferenceReceipt("model", "provider")
    with observe_inference(lifecycle, collector):
        assert lifecycle.has_hook("pre_api_request")
        if type(count) is int and count == 0:
            lifecycle.invoke_hook("pre_api_request", tool_count=count, model='model', provider='provider')
        else:
            with pytest.raises(SystemExit):
                lifecycle.invoke_hook("pre_api_request", tool_count=count, model='model', provider='provider')
    assert (lifecycle.has_hook, lifecycle.invoke_hook) == original


def test_phase_observer_receives_no_prompt_or_response_content():
    lifecycle = SimpleNamespace(has_hook=lambda _: False, invoke_hook=lambda *a, **k: [])
    phases = []
    value = {'model': 'model', 'provider': 'provider', 'schema_version': 2}

    def entry(**kwargs):
        lifecycle.invoke_hook('pre_api_request', tool_count=0, model='model', provider='provider')
        lifecycle.invoke_hook('post_api_request', **event(response={'private': 'SECRET'}))
        print('{"result":"SECRET"}')

    execute(value, {'protocol': 2, 'prompt': 'PRIVATE', 'reasoning': 'medium',
                    'expected_manifest_digest': digest(value)},
            entry=entry, lifecycle=lifecycle, progress=phases.append)
    assert phases == ['cli_started', 'request_validated', 'response_received', 'result_parsed']


@pytest.mark.parametrize("exit_code", ["return", None, 0, 1, 2, "0", False])
def test_receipt_mode_observes_call_and_restores_hook_without_leaking_content(exit_code):
    lifecycle = SimpleNamespace(has_hook=lambda name: False, invoke_hook=lambda name, **kwargs: [])
    originals = lifecycle.has_hook, lifecycle.invoke_hook
    value = {"model": "model", "provider": "provider", "schema_version": 2}

    def entry(**kwargs):
        assert lifecycle.has_hook("post_api_request")
        lifecycle.invoke_hook("post_api_request", **event(response={"secret": "PRIVATE"}, base_url="PRIVATE"))
        print(json.dumps({"knowledge_id": "known", "confidence": 0.9}))
        if exit_code != "return":
            raise SystemExit(exit_code)

    if exit_code not in ("return", None) and not (type(exit_code) is int and exit_code == 0):
        with pytest.raises(SystemExit):
            execute(value, {"protocol": 2, "prompt": "question", "reasoning": "low",
                           "expected_manifest_digest": digest(value)}, entry=entry, lifecycle=lifecycle)
        assert (lifecycle.has_hook, lifecycle.invoke_hook) == originals
        return
    result = execute(value, {"protocol": 2, "prompt": "question", "reasoning": "low",
                            "expected_manifest_digest": digest(value)}, entry=entry, lifecycle=lifecycle)
    assert result["receipt"]["response_model"] == "model"
    assert result["receipt"]["usage"] is None
    assert "PRIVATE" not in str(result)
    assert (lifecycle.has_hook, lifecycle.invoke_hook) == originals


@pytest.mark.parametrize("changes", [{"response_model": None}, {"response_model": "other"},
    {"session_id": ""}, {"api_call_count": 2}, {"assistant_tool_call_count": 1},
    *[{"finish_reason": reason} for reason in (None, "", "length", "incomplete", "content_filter", "tool_calls", [], True)]])
def test_invalid_receipt_rejected(changes):
    collector = InferenceReceipt("model", "provider")
    collector.observe(**{**event(), **changes})
    with pytest.raises(BridgeError):
        collector.finish()


def test_duplicate_and_missing_receipts_rejected():
    collector = InferenceReceipt("model", "provider")
    with pytest.raises(BridgeError):
        collector.finish()
    collector.observe(**event())
    collector.observe(**event())
    with pytest.raises(BridgeError):
        collector.finish()


def test_tool_response_aborts_before_cli_dispatch_and_restores_hooks():
    lifecycle = SimpleNamespace(has_hook=lambda name: False, invoke_hook=lambda name, **kwargs: [])
    originals = lifecycle.has_hook, lifecycle.invoke_hook
    effects = []
    value = {"model": "model", "provider": "provider", "schema_version": 2}

    def entry(**kwargs):
        try:
            lifecycle.invoke_hook("post_api_request", **{**event(), "assistant_tool_call_count": 1})
        except Exception:  # noqa: BLE001, S110 - reproduce Hermes hook suppression
            pass  # Same suppression boundary as the checked Hermes source.
        effects.append("skill_manage dispatched")

    with pytest.raises(SystemExit):
        execute(value, {"protocol": 2, "prompt": "fixture", "reasoning": "low",
                       "expected_manifest_digest": digest(value)}, entry=entry, lifecycle=lifecycle)
    assert effects == []
    assert (lifecycle.has_hook, lifecycle.invoke_hook) == originals


@pytest.mark.parametrize("failure", [None, "request", "manifest", "model", "missing", "truncated", "no_finish"])
def test_semantic_transport_requires_current_receipt(monkeypatch, failure):
    from k3_support import semantic

    identity = {"schema_version": 2, "model": "model", "provider": "provider"}
    def run(argv, **kwargs):
        payload = json.loads(kwargs["input"])
        assert payload["protocol"] == 2
        receipt = {"api_request_id": "id", "session_id": "session", "model": "model",
                   "response_model": "model", "provider": "provider", "usage": None, "billing_verified": False,
                   "finish_reason": "stop"}
        value = {"protocol": 2, "result": {"answer": "fixture"}, "receipt": receipt,
                 "manifest_digest": digest(identity), "request_digest": digest(payload)}
        if failure == "request": value["request_digest"] = "old-input"
        if failure == "manifest": value["manifest_digest"] = "old-config"
        if failure == "model": receipt["response_model"] = "different"
        if failure == "missing": value.pop("receipt")
        if failure == "truncated": receipt["finish_reason"] = "length"
        if failure == "no_finish": receipt.pop("finish_reason")
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    monkeypatch.setattr(semantic.subprocess, "run", run)
    identity_token = semantic.expected_bridge_identity.set(identity)
    manifest_token = semantic.expected_bridge_manifest.set(digest(identity))
    try:
        result = semantic._hermes_json("question", reasoning="low", timeout=1)
        assert result == ({"answer": "fixture"} if failure is None else None)
    finally:
        semantic.expected_bridge_manifest.reset(manifest_token)
        semantic.expected_bridge_identity.reset(identity_token)
