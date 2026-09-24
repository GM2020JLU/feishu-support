from types import SimpleNamespace
import json

from test_knowledge_runtime import entry
from k3_support import cli


def test_explicit_corpus_commands_do_not_publish_or_migrate(conn, config, monkeypatch, capsys):
    entry(conn)
    monkeypatch.setattr(cli, '_config', lambda args: config)
    before = conn.serialize()
    cli.cmd_knowledge_corpus(SimpleNamespace(corpus_build=False))
    status = json.loads(capsys.readouterr().out)
    assert not status['current'] and not status['release_authorized']
    assert conn.serialize() == before
    cli.cmd_knowledge_corpus(SimpleNamespace(corpus_build=True))
    result = json.loads(capsys.readouterr().out)
    assert result['built'] and not result['release_authorized']
    cli.cmd_knowledge_corpus(SimpleNamespace(corpus_build=False))
    assert json.loads(capsys.readouterr().out)['current']
