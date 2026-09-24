import json
from types import SimpleNamespace

from k3_support.case_detail import _model_observation_lines


def test_model_display_uses_only_observed_allowlisted_fields():
    calls = []
    rows = [{"created_at": "then", "knowledge_runtime_json": json.dumps({
        "knowledge_selection_observation": {"verification": "bridge_sdk_observation",
            "model": "observed-model", "provider": "observed-provider", "raw": "PRIVATE"}})},
        {"created_at": "later", "knowledge_runtime_json": json.dumps({
            "knowledge_runtime_binding": {"selection": {"model": "configured-only"}}})}]
    def execute(sql, params):
        calls.append(params)
        return SimpleNamespace(fetchall=lambda: rows)
    text = "\n".join(_model_observation_lines(SimpleNamespace(execute=execute), "K3-target"))
    assert calls == [("K3-target",)]
    assert "observed-model" in text and "observed-provider" in text
    assert "configured-only" not in text and "PRIVATE" not in text
    assert "无已记录的 SDK 模型观测" in text
    assert "不解除知识发布" in text
