import copy

import pytest
from test_replay_history import request

from k3_support import replay_history as history


def local_executor(monkeypatch):
    """Exercise production routing in memory, not external processes."""
    import json

    def process(**kw):
        path = f"/proc/self/fd/{kw['pass_fds'][0]}"
        probe = "probe=True" in kw["argv"][-1]
        return json.dumps(
            history.execute_snapshot(json.loads(kw["stdin"]), path, probe=probe)
        )

    monkeypatch.setattr(history, "run_process", process)


@pytest.mark.parametrize('at,outside', [('2026-09-09T08:59:59+08:00', True), ('2026-09-09T09:00:00+08:00', False), ('2026-09-09T17:59:59+08:00', False), ('2026-09-09T18:00:00+08:00', True)])
def test_snapshot_business_clock_work_boundaries(conn, config, monkeypatch, at, outside):
    from k3_support.timeutil import parse_iso
    local_executor(monkeypatch)
    value = request(config)
    proposal = value.pop('proposal')
    value['assumptions'] = {'observed_at': at}
    value['event']['occurred_at'] = at
    before = conn.serialize()
    result = history.run_snapshot_inference(config.database_path, value, router=lambda _: proposal)
    assert result['business_window']['active'] is outside
    assert 'not_historical' in result['clock_scope']
    rows = result['report']['intentions']['outbox']
    assert rows
    assert all(parse_iso(row['created_at']) == parse_iso(at) for row in rows)
    assert conn.serialize() == before


@pytest.mark.parametrize('at', ['2026-09-09T10:00:00', 'yesterday', 123, '', '2026-13-09T10:00:00Z'])
def test_invalid_clock_rejected_before_copy(config, monkeypatch, at):
    value = request(config)
    value.pop('proposal')
    value['assumptions'] = {'observed_at': at}
    monkeypatch.setattr(history, '_snapshot_data', lambda *args: pytest.fail('snapshot accessed'))
    with pytest.raises(ValueError):
        history.run_snapshot_inference(config.database_path, value, router=lambda _: pytest.fail('model called'))


@pytest.mark.parametrize('mode', ['observe', 'collaborate', 'auto_60', 'auto', 'paused', 'stopped'])
def test_explicit_assumptions_only_change_snapshot(conn, config, monkeypatch, mode):
    local_executor(monkeypatch)
    value = request(config)
    proposal = value.pop('proposal')
    value['assumptions'] = {'relationship': 'supervisor', 'function_role': 'project_manager', 'mode': mode}
    before = conn.serialize()
    seen = []

    def router(context):
        seen.append(context)
        return proposal

    result = history.run_snapshot_inference(config.database_path, value, router=router)
    assert result['assumptions'] == value['assumptions']
    if mode in {'paused', 'stopped'}:
        assert not seen
        assert result['report']['result']['blocked_by_mode'] == mode
    else:
        assert len(seen) == 1
        assert seen[0]['requester_profile']['relationship'] == 'supervisor'
        assert seen[0]['requester_profile']['function_role'] == 'project_manager'
    assert conn.serialize() == before


@pytest.mark.parametrize('assumptions', [None, [], {'mode': 'invalid'}, {'mode': []}, {'relationship': 'peer'}, {'expected_route': 'direct_answer'}])
def test_invalid_assumptions_rejected_before_snapshot_or_model(config, monkeypatch, assumptions):
    value = request(config)
    value.pop('proposal')
    value['assumptions'] = assumptions
    monkeypatch.setattr(history, '_snapshot_data', lambda *args: pytest.fail('snapshot accessed'))
    with pytest.raises(ValueError):
        history.run_snapshot_inference(config.database_path, value, router=lambda _: pytest.fail('model called'))


def test_inference_uses_production_input_and_one_frozen_snapshot(
    conn, config, monkeypatch
):
    local_executor(monkeypatch)
    value = request(config)
    proposal = value.pop("proposal")
    seen = []
    before = conn.serialize()

    def router(context):
        seen.append(copy.deepcopy(context))
        # Callback mutation must not rewrite captured input or replay config.
        context.clear()
        return proposal

    result = history.run_snapshot_inference(config.database_path, value, router=router)
    assert len(seen) == 1
    assert set(seen[0]) == {
        "message",
        "source",
        "chat_type",
        "baseline",
        "requester_profile",
        "audience_strategy",
        "approved_knowledge",
        "recent_conversation",
    }
    assert result["report"]["result"]["route"]["route"] == "owner_decision"
    assert result["model_callback_invoked"] is True
    assert result["model_invoked"] is None  # Callback success is not provider proof.
    assert "routing_inputs" not in result
    assert conn.serialize() == before


@pytest.mark.parametrize("extra", ["proposal", "expected_route", "gold", "predictions"])
def test_labels_rejected_before_database_or_model(config, monkeypatch, extra):
    value = request(config)
    value.pop("proposal")
    value[extra] = "owner_decision"
    monkeypatch.setattr(
        history, "_snapshot_data", lambda *a: pytest.fail("source accessed")
    )
    with pytest.raises(ValueError):
        history.run_snapshot_inference(
            config.database_path, value, router=lambda _: pytest.fail("model called")
        )


def test_failed_router_not_retried(conn, config, monkeypatch):
    local_executor(monkeypatch)
    value = request(config)
    value.pop("proposal")
    calls = []

    def router(context):
        calls.append(1)

    with pytest.raises(ValueError):
        history.run_snapshot_inference(config.database_path, value, router=router)
    assert calls == [1]


def test_changed_observations_rejected(config, monkeypatch):
    value = request(config)
    proposal = value.pop("proposal")
    snapshots = []
    frozen = b"fixed snapshot"
    monkeypatch.setattr(history, "_snapshot_data", lambda _: frozen)

    def execute(data, request, **kwargs):
        snapshots.append(data)
        return {"routing_inputs": [{"message": str(len(snapshots))}]}

    monkeypatch.setattr(history, "_run_snapshot_data", execute)
    with pytest.raises(ValueError, match="observations changed"):
        history.run_snapshot_inference(
            config.database_path, value, router=lambda _: proposal
        )
    assert snapshots == [frozen, frozen]
