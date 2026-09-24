import io
import json

import pytest

from k3_support import replay_cli


@pytest.mark.parametrize('arguments', [
    ['--infer-pipeline'], ['--review-clarification'],
    ['--snapshot', '/unused.db', '--infer-pipeline', '--infer-route']])
def test_pipeline_requires_explicit_unambiguous_scope(monkeypatch, arguments):
    monkeypatch.setattr('sys.stdin', None)
    with pytest.raises(SystemExit) as error:
        replay_cli.main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize('arguments', [
    ['--infer-debug'], ['--snapshot', '/unused.db', '--infer-debug', '--infer-route'],
    ['--snapshot', '/unused.db', '--infer-debug', '--infer-pipeline']])
def test_debug_cli_requires_explicit_exclusive_scope(monkeypatch, arguments):
    monkeypatch.setattr('sys.stdin', None)
    with pytest.raises(SystemExit) as error:
        replay_cli.main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize('simulated', [False, True])
def test_debug_cli_reuses_sealed_review_and_redacts(conn, config, monkeypatch, capsys, simulated):
    from test_replay_debug_snapshot import local_executor, request, wait_decision
    from test_review import result_text
    value = request(conn, config)
    if simulated:
        case = conn.execute('SELECT case_id FROM jobs').fetchone()[0]
        conn.execute("UPDATE jobs SET state='queued',available_at='2020-01-01T00:00:00+00:00'")
        value['execution'] = {'report': result_text(case), 'exit_status': 0}
    local_executor(monkeypatch)
    calls = []
    def reviewer(prompt, **kwargs):
        calls.append(kwargs)
        return json.loads(wait_decision({'prompt': prompt}))
    monkeypatch.setattr('k3_support.semantic._hermes_json', reviewer)
    monkeypatch.setattr('sys.stdin', io.TextIOWrapper(io.BytesIO(json.dumps(value).encode())))
    before = conn.serialize()
    assert replay_cli.main(['--snapshot', str(config.database_path), '--infer-debug',
                            '--inference-timeout', '17']) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report['review_ok'] and report['model_calls'] == 1
    assert report['completion'] == ('review_pending' if simulated else None)
    assert not report['content_included'] and not report['external_consumers']
    assert 'Caller failure' not in output.out and 'buildhost' not in output.out
    assert calls == [{'reasoning': 'medium', 'timeout': 17}]
    assert conn.serialize() == before


def test_cli_conversation_uses_pipeline_and_redacts_private_text(conn, config, monkeypatch, capsys):
    from test_replay_model_pipeline import local_executor, request
    from test_workflow_replay import group_event
    from test_routing import route_value
    local_executor(monkeypatch)
    value = request(config)
    value.pop('event')
    value['events'] = [group_event(1, 'private-first'),
                       group_event(2, 'private-followup', parent='om_group_1')]
    calls = []
    def router(context, **kwargs):
        calls.append(context)
        return route_value('owner_decision')
    monkeypatch.setattr(replay_cli, 'hermes_message_router', router)
    monkeypatch.setattr('sys.stdin', io.TextIOWrapper(io.BytesIO(json.dumps(value).encode())))
    before = conn.serialize()
    assert replay_cli.main(['--snapshot', str(config.database_path), '--infer-pipeline']) == 0
    output = capsys.readouterr()
    assert 'private-first' not in output.out + output.err
    assert 'private-followup' not in output.out + output.err
    report = json.loads(output.out)
    assert len(report['turns']) == 2 and len(calls) == 2
    assert all(turn['model_invoked'] is None and turn['model_callback_invoked']
               for turn in report['turns'])
    assert not report['content_included'] and not report['external_consumers']
    assert conn.serialize() == before


def test_default_output_hides_contents_and_identity(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    result = {
        "steps": [
            {
                "result": {"route": {"route": "owner_decision"}, "private": "secret"},
                "intentions": {"outbox": [{"body": "secret"}], "jobs": []},
            }
        ],
        "scope": "fixture",
        "knowledge_scope": "empty_database",
        "profile_scope": "assumed",
    }
    monkeypatch.setattr(replay_cli, "run_isolated_scenario", lambda *a, **k: result)
    assert replay_cli.main([]) == 0
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err
    report = json.loads(output.out)
    assert report["steps"][0]["outbox_intentions"] == 1
    assert report["content_included"] is False


def test_summary_reasons_are_allowlisted_not_private_model_text():
    report = replay_cli.summary({'scope': 'fixture', 'knowledge_scope': 'fixture',
        'profile_scope': 'fixture', 'steps': [{'result': {'route': {
            'route': 'owner_decision', 'reason_codes': ['requires_commitment',
                'private-person-and-question', {'secret': True}, 'requires_commitment']}},
            'intentions': {'outbox': [], 'jobs': []}}]})
    assert report['steps'][0]['reason_codes'] == ['requires_commitment']
    assert report['steps'][0]['reason_labels'] == ['可能涉及承诺或排期']
    assert 'private-person' not in json.dumps(report) and 'secret' not in json.dumps(report)


@pytest.mark.parametrize("data", [b"not JSON private", b"x" * 262145])
def test_invalid_input_never_starts_sandbox(monkeypatch, capsys, data):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(data)))
    monkeypatch.setattr(
        replay_cli,
        "run_isolated_scenario",
        lambda *a, **k: pytest.fail("unexpected launch"),
    )
    assert replay_cli.main([]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "private" not in output.err


def test_sandbox_failure_is_not_retried_or_disclosed(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr(replay_cli, "run_isolated_scenario", fail)
    assert replay_cli.main([]) == 1
    assert calls == [1]
    assert "private" not in capsys.readouterr().err


def test_content_requires_explicit_flag(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    monkeypatch.setattr(
        replay_cli, "run_isolated_scenario", lambda *a, **k: {"private": "test message"}
    )
    assert replay_cli.main(["--include-content"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["content_included"] is True
    assert output["result"]["private"] == "test message"


def test_snapshot_cli_is_explicit_and_redacted(monkeypatch, capsys, tmp_path):
    source = tmp_path / "snapshot.db"
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b'{"event":{}}')))
    calls = []

    def replay(path, request, **kwargs):
        calls.append((path, request, kwargs))
        return {
            "report": {
                "result": {"route": {"route": "research"}, "body": "secret"},
                "intentions": {"outbox": [{"body": "secret"}], "jobs": []},
            },
            "scope": "new_event_against_current_snapshot_not_historical_time_travel",
        }

    monkeypatch.setattr(replay_cli, "run_snapshot_replay", replay)
    monkeypatch.setattr(
        replay_cli,
        "run_isolated_scenario",
        lambda *a, **k: pytest.fail("wrong backend"),
    )
    assert replay_cli.main(["--snapshot", str(source)]) == 0
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err
    assert calls == [(source, {"event": {}}, {"timeout": 30})]
    result = json.loads(output.out)
    assert result["knowledge_scope"] == "current_snapshot"
    assert result["external_consumers"] is False


def test_inference_cli_routes_only_with_explicit_flag(monkeypatch, capsys, tmp_path):
    source = tmp_path / "snapshot.db"
    monkeypatch.setattr(
        "sys.stdin", io.TextIOWrapper(io.BytesIO(b'{"config":{},"event":{}}'))
    )
    calls = []

    def model(value, **kwargs):
        calls.append((value, kwargs))
        return {"route": "research"}

    def replay(path, request, *, router, timeout):
        assert path == source
        assert set(request) == {"config", "event"}
        assert router({"message": "private"}) == {"route": "research"}
        return {
            "report": {
                "result": {"route": {"route": "research"}},
                "intentions": {"outbox": [], "jobs": []},
            },
            "scope": "snapshot",
            "model_invoked": None,
            "model_callback_invoked": True,
            "provider_verification": "not_established_by_callback",
        }

    monkeypatch.setattr(replay_cli, "run_snapshot_inference", replay)
    monkeypatch.setattr(replay_cli, "hermes_message_router", model)
    assert (
        replay_cli.main(
            ["--snapshot", str(source), "--infer-route", "--inference-timeout", "12"]
        )
        == 0
    )
    output = capsys.readouterr()
    assert "private" not in output.out + output.err
    assert json.loads(output.out)["model_invoked"] is None
    assert calls == [({"message": "private"}, {"timeout": 12})]


@pytest.mark.parametrize(
    "args",
    [["--infer-route"], ["--inference-timeout", "0"], ["--inference-timeout", "301"]],
)
def test_inference_cli_invalid_options(args):
    with pytest.raises(SystemExit) as exc:
        replay_cli.main(args)
    assert exc.value.code == 2


@pytest.mark.parametrize("timeout", ["nan", "inf", "0", "-1", "301"])
def test_invalid_timeout_is_rejected_before_launch(monkeypatch, timeout):
    monkeypatch.setattr(
        replay_cli,
        "run_isolated_scenario",
        lambda *a, **k: pytest.fail("unexpected launch"),
    )
    with pytest.raises(SystemExit) as exc:
        replay_cli.main(["--timeout", timeout])
    assert exc.value.code == 2
