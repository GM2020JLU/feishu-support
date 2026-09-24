import io
import json
from pathlib import Path

import pytest

from k3_support import cli, replay_recorded


def arguments():
    return ['--config', '/nonexistent/no-live-config', 'recorded-replay',
            '--database', '/fixture/snapshot.db', '--package', '/fixture/k3_support',
            '--site-packages', '/fixture/site-packages', '--runtime-digest', 'a'*64,
            '--snapshot-digest', 'b'*64, '--request-digest', 'c'*64]


def test_cli_uses_only_explicit_inputs(monkeypatch, capsys):
    request = {'config': {}, 'event': {'content': 'private fixture'}, 'proposal': {}}
    monkeypatch.setattr(cli.sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(request).encode())))
    monkeypatch.setattr(cli, '_config', lambda *a: pytest.fail('must not read live config'))
    def run(database, value, **kwargs):
        assert database == Path('/fixture/snapshot.db')
        assert value == request
        assert kwargs['package'] == Path('/fixture/k3_support')
        assert kwargs['site_packages'] == Path('/fixture/site-packages')
        assert kwargs['expected'] == dict(runtime='a'*64, snapshot='b'*64, request='c'*64)
        assert kwargs['timeout'] == 30
        return {'release_authorized': False}
    monkeypatch.setattr(replay_recorded, 'run_recorded', run)
    assert cli.main(arguments()) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {'release_authorized': False}
    assert 'private fixture' not in output.out + output.err


@pytest.mark.parametrize('payload', [b'x'*262145, b'{broken'])
def test_cli_rejects_bad_input_before_runner(monkeypatch, capsys, payload):
    monkeypatch.setattr(cli.sys, 'stdin', io.TextIOWrapper(io.BytesIO(payload)))
    monkeypatch.setattr(replay_recorded, 'run_recorded', lambda *a, **k: pytest.fail('must not execute'))
    assert cli.main(arguments()) == 2
    assert json.loads(capsys.readouterr().err)['ok'] is False
