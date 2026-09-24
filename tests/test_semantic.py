from __future__ import annotations

import subprocess

from k3_support.semantic import hermes_semantic_selector


def test_hermes_semantic_selector_requires_strict_json(monkeypatch):
    calls = []
    inputs = []

    def run(*args, **kwargs):
        calls.append(args[0])
        inputs.append(kwargs.get("input"))
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"knowledge_id":"knw_1","confidence":0.91}',
            stderr="",
        )

    monkeypatch.setattr("k3_support.semantic.subprocess.run", run)
    result = hermes_semantic_selector(
        "风扇太吵",
        [{"knowledge_id": "knw_1", "title": "风扇配置"}],
        executable="/opt/k3/hermes",
    )
    assert result == {"knowledge_id": "knw_1", "confidence": 0.91}
    assert calls[0][0] == "/opt/k3/hermes"
    assert calls[0][1:] == ["--support-json-stdin"]
    assert "风扇太吵" not in " ".join(calls[0])
    assert "风扇太吵" in inputs[0]


def test_hermes_semantic_selector_fails_closed(monkeypatch):
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="not-json", stderr=""
        )

    monkeypatch.setattr("k3_support.semantic.subprocess.run", run)
    assert hermes_semantic_selector("任意问题", []) is None
