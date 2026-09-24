import json
import os

import pytest
import yaml
from test_draft_retention import seed

from k3_support import cli


def run(config, capsys, command, identifier, *extra):
    config.path.write_text(yaml.safe_dump(config.raw))
    code = cli.main(['--config', str(config.path), command,
                     '--candidate-id', identifier, '--days', '30', *extra])
    out = capsys.readouterr()
    return code, json.loads(out.err if code else out.out)


def test_cli_preview_then_explicit_clear_is_bound_and_audited(conn, config, capsys, monkeypatch):
    monkeypatch.setattr(cli, 'migrate', lambda *a: pytest.fail('unexpected migration'))
    identifier = seed(conn)
    before = conn.serialize()
    code, value = run(config, capsys, 'draft-retention-preview', identifier)
    assert code == 0 and value['eligible'] and conn.serialize() == before
    code, _ = run(config, capsys, 'draft-retention-clear', identifier,
                  '--preview-digest', value['row_digest'])
    assert code == 2 and conn.serialize() == before
    code, result = run(config, capsys, 'draft-retention-clear', identifier,
                       '--preview-digest', value['row_digest'], '--confirm-logical-delete')
    assert code == 0 and result['logical_delete_only'] and not result['secure_erasure']
    reason = json.loads(conn.execute('SELECT reason FROM retention_tombstones').fetchone()[0])
    assert reason['actor_id'] == f'local-os-uid:{os.getuid()}'


@pytest.mark.parametrize('command', ['draft-retention-preview', 'draft-retention-clear'])
def test_missing_database_never_created(config, capsys, command):
    args = ['--preview-digest', 'a' * 64, '--confirm-logical-delete'] if command.endswith('clear') else []
    code, result = run(config, capsys, command, 'missing', *args)
    assert code == 2 and result['error'] == 'FileNotFoundError'
    assert not config.database_path.exists()


def test_old_database_not_migrated(conn, config, capsys):
    conn.execute('DELETE FROM schema_migrations WHERE version=86')
    before = conn.serialize()
    code, _ = run(config, capsys, 'draft-retention-preview', 'missing')
    assert code == 2 and conn.serialize() == before
