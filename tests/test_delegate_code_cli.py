import json
import os

import yaml
from test_review import active_config

from k3_support.cli import main
from k3_support.ids import digest
from k3_support.store import claim_jobs, create_case


def test_operator_cli_creates_bound_task_without_running_agent(conn, config, tmp_path, capsys):
    cfg = active_config(config)
    cfg.path.write_text(yaml.safe_dump(cfg.raw))
    case, _ = create_case(conn, title='fixture', case_type='investigation', severity='P2', confidence=.8)
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (case,))
    directory = tmp_path / 'deployment'
    directory.mkdir(mode=0o700)
    contract = {'version': 2, 'agent': 'claude', 'provider': 'fixture',
                'base_url': 'https://provider.example', 'model': 'bound-model',
                'reasoning': 'high', 'wire_api': 'messages'}
    (directory / 'execution-contract.json').write_text(json.dumps(contract))
    brief = tmp_path / 'brief.md'
    brief.write_text('UNTRUSTED INPUT\nFORBIDDEN ACTIONS\nACCEPTANCE TESTS')
    arguments = ['--config', str(cfg.path), 'delegate-code', case, '--repo', 'u-boot',
                 '--brief', str(brief), '--contract-directory', str(directory),
                 '--worker-uid', str(os.geteuid()+1)]
    assert main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['execution'] == {'agent': 'claude', 'contract_fingerprint': digest(contract)}
    row = conn.execute('SELECT * FROM jobs WHERE job_id=?', (result['job_id'],)).fetchone()
    assert row['state'] == 'queued'
    assert claim_jobs(conn, 'legacy-worker', job_types=('codex',)) == []
    payload = json.loads(conn.execute('SELECT payload_json FROM broker_inputs').fetchone()[0])
    assert payload['context_extra']['execution'] == result['execution']
    assert main(arguments) == 0
    assert not json.loads(capsys.readouterr().out)['created']
    assert conn.execute('SELECT count(*) FROM broker_execution_starts').fetchone()[0] == 0
