"""Offline guardrail scenarios, not model predictions or send authorization."""

import math

from .routing import (
    FUNCTION_ROLES,
    RELATIONSHIPS,
    SEVERITIES,
    choose_route,
    unknown_profile,
    validate_route_output,
)


def simulate(payload, *, minimum_confidence):
    _threshold(minimum_confidence)
    if not isinstance(payload, dict) or set(payload) != {
        "query",
        "relationship",
        "function_role",
        "severity",
        "proposal",
    }:
        raise ValueError("需要问题、假设角色、严重程度和完整路由建议")
    query = payload["query"]
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 4000:
        raise ValueError("问题长度必须为 1–4000 字符")
    if (
        any(
            not isinstance(payload[key], str)
            for key in ("relationship", "function_role", "severity")
        )
        or payload["relationship"] not in RELATIONSHIPS
        or payload["function_role"] not in FUNCTION_ROLES
        or payload["severity"] not in SEVERITIES
    ):
        raise ValueError("无效的假设角色或严重程度")
    proposal = validate_route_output(payload["proposal"])
    profile = unknown_profile(None)
    profile.update(
        relationship=payload["relationship"], function_role=payload["function_role"]
    )
    result = choose_route(
        query=query,
        source="feishu_im",
        chat_type="p2p",
        baseline={"severity": payload["severity"]},
        profile=profile,
        knowledge=None,
        router=lambda _: proposal,
        minimum_confidence=minimum_confidence,
    )
    return {
        "result": result,
        "read_only": True,
        "model_invoked": False,
        "minimum_confidence": minimum_confidence,
        "assumptions": {
            "relationship": payload["relationship"],
            "function_role": payload["function_role"],
            "knowledge": "未检索，不假设有可直接回复的知识",
            "conversation": "无历史上下文",
        },
        "scope": "仅路由规则试算，不验证发送权限、自动追问审批或真实 AI 判断",
    }


def _threshold(value):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError("置信度阈值必须为 0 到 1 的有限数值")


def compare(payload, *, minimum_confidence):
    if not isinstance(payload, dict) or set(payload) != {
        "scenario",
        "proposed_minimum_confidence",
    }:
        raise ValueError("需要同一场景与拟调整的路由阈值")
    proposed = payload["proposed_minimum_confidence"]
    _threshold(proposed)
    current = simulate(payload["scenario"], minimum_confidence=minimum_confidence)
    candidate = simulate(payload["scenario"], minimum_confidence=proposed)
    changed = [
        key
        for key in current["result"]
        if current["result"][key] != candidate["result"][key]
    ]
    return {
        "current": current,
        "candidate": candidate,
        "changed_fields": changed,
        "read_only": True,
        "applied": False,
        "scope": "仅比较路由置信度阈值；相同结果不证明整体配置安全或质量达标",
    }
