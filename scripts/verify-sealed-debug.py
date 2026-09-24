#!/usr/bin/env python3
"""Developer verifier: real bwrap, test fixtures only, no external consumers.

Requires the checkout's test dependencies. Kept separate from pytest because
the default test-suite process guard intentionally disallows bwrap.
"""
import json
import sys
import tempfile
from pathlib import Path

from k3_support.config import Config, validate_config
from k3_support.db import connect, migrate
from k3_support.replay_debug_snapshot import run

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from conftest import config_data  # noqa: E402
from test_replay_debug_snapshot import request, wait_decision  # noqa: E402
from test_review import result_text  # noqa: E402


def verify():
    with tempfile.TemporaryDirectory(prefix='codex-sealed-debug-') as directory:
        root = Path(directory)
        config = Config(validate_config(config_data(root)), root / 'config.yaml')
        conn = connect(config.database_path)
        try:
            migrate(conn)
            value = request(conn, config)
            case = conn.execute('SELECT case_id FROM jobs WHERE job_id=?', (value['job_id'],)).fetchone()[0]
            conn.execute("UPDATE jobs SET state='queued',available_at='2020-01-01T00:00:00+00:00' WHERE job_id=?",
                         (value['job_id'],))
            before = conn.serialize()
            outcomes = []
            for status, expected in [(None, 'unverified'), (1, 'execution_failed'), (0, 'review_pending')]:
                value['execution'] = {'report': result_text(case), 'exit_status': status}
                result = run(config.database_path, value, reviewer=wait_decision)
                assert result['result']['completion']['state'] == expected
                assert len(result['calls']) == int(status == 0)
                if status == 0:
                    assert result['result']['review']['review']['ok']
                    assert result['transcript']['remaining'] == 0
                assert not result['result']['external_consumers']
                assert conn.serialize() == before
                outcomes.append(expected)
            return {'ok': True, 'real_sandbox': True, 'real_model': False,
                    'consumers_started': False, 'source_unchanged': True, 'outcomes': outcomes}
        finally:
            conn.close()


if __name__ == '__main__':
    print(json.dumps(verify()))
