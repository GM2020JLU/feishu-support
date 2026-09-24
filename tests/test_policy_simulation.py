import pytest
from test_routing import route_value

from k3_support.policy_simulation import compare, simulate


def test_comparison_uses_same_scenario_without_mutation():
    import copy

    payload = {"scenario": scenario(), "proposed_minimum_confidence": 0.99}
    before = copy.deepcopy(payload)
    result = compare(payload, minimum_confidence=0.9)
    assert result["current"]["result"]["route"] == "codex_debug"
    assert result["candidate"]["result"]["route"] == "research"
    assert "route" in result["changed_fields"] and not result["applied"]
    assert payload == before


@pytest.mark.parametrize(
    "value", [True, None, "0.9", -1, 2, float("nan"), float("inf")]
)
def test_invalid_comparison_threshold(value):
    with pytest.raises(ValueError):
        compare(
            {"scenario": scenario(), "proposed_minimum_confidence": value},
            minimum_confidence=0.9,
        )


def scenario(**overrides):
    return {
        "query": "启动失败",
        "relationship": "peer",
        "function_role": "engineering",
        "severity": "P2",
        "proposal": route_value("codex_debug"),
        **overrides,
    }


@pytest.mark.parametrize(
    "values,expected",
    [
        ({}, "codex_debug"),
        ({"severity": "P0"}, "urgent_notify"),
        ({"relationship": "external"}, "owner_decision"),
        (
            {
                "proposal": route_value(
                    "direct_answer", reason_codes=["approved_knowledge_match"]
                )
            },
            "research",
        ),
        (
            {"function_role": "project_manager", "query": "什么时候交付"},
            "owner_decision",
        ),
    ],
)
def test_simulation_uses_real_guardrails(values, expected):
    result = simulate(scenario(**values), minimum_confidence=0.9)
    assert result["result"]["route"] == expected
    assert result["read_only"] and not result["model_invoked"]


@pytest.mark.parametrize(
    "values", [{"relationship": []}, {"query": ""}, {"severity": "bad"}]
)
def test_bad_scenario_rejected(values):
    with pytest.raises(ValueError):
        simulate(scenario(**values), minimum_confidence=0.9)


@pytest.mark.parametrize(
    "field,value",
    [
        ("route", []),
        ("conversation_relation", {}),
        ("issue_type", []),
        ("severity", {}),
        ("reason_codes", [[]]),
        ("fallback_route", []),
    ],
)
def test_malformed_proposal_is_validation_error(field, value):
    proposal = (
        route_value("codex_debug", **{field: value})
        if field != "route"
        else route_value(value)
    )
    with pytest.raises(ValueError):
        simulate(scenario(proposal=proposal), minimum_confidence=0.9)
