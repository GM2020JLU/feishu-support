import json
from pathlib import Path
from uuid import uuid4

import yaml

from k3_support import cli
from k3_support.operations import apply_retention
from test_retention_recheck import candidate


def test_cli_preview_confirm_execute_and_inspect_temporary_fixture(conn, config, capsys):
    config.path.write_text(yaml.safe_dump(config.raw))
    _, _, candidates = candidate(conn, config)
    apply_retention(conn, config, candidates)
    conn.execute("UPDATE retention_attempts SET updated_at='2020-01-01T00:00:00+00:00'")
    row = conn.execute('SELECT * FROM retention_attempts').fetchone()
    base = ['--config', str(config.path)]
    before = conn.serialize()
    assert cli.main(base + ['retention-purge-preview', '--attempt-id', row['attempt_id'], '--days', '30']) == 0
    shown = json.loads(capsys.readouterr().out)
    assert conn.serialize() == before and not shown['deletion_authorized']
    request = str(uuid4())
    prepare = base + ['retention-purge-prepare', '--attempt-id', row['attempt_id'], '--days', '30',
                      '--request-id', request, '--binding-digest', shown['binding_digest']]
    assert cli.main(prepare) == 2
    capsys.readouterr()
    assert cli.main(prepare + ['--confirm-permanent-delete']) == 0
    assert json.loads(capsys.readouterr().out)['files_deleted'] == 0
    assert Path(row['quarantine_path']).exists()
    assert cli.main(base + ['retention-purge-execute', '--request-id', request]) == 0
    assert json.loads(capsys.readouterr().out)['files_deleted'] == 1
    assert not Path(row['quarantine_path']).exists()
    before = conn.serialize()
    assert cli.main(base + ['retention-purge-inspect', '--request-id', request]) == 0
    assert json.loads(capsys.readouterr().out)['request_state'] == 'purged'
    assert conn.serialize() == before
