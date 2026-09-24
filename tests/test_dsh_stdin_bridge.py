import subprocess
from pathlib import Path

import pytest

BRIDGE = Path(__file__).parents[1] / "src/k3_support/agent_bridges/dsh-stdin.mjs"


def launch(tmp_path, task, code=None):
    loader = tmp_path / "loader.mjs"
    loader.write_text(code or """
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
const task = process.argv[6];
assert.deepEqual(process.argv.slice(2, 6), ['--profile', 'support-headless', '--', '--']);
assert.ok(!readFileSync('/proc/self/cmdline').includes(Buffer.from(task)));
process.stdout.write(task);
""")
    return subprocess.run(
        ["node", str(BRIDGE), str(loader), "support-headless"], input=task,
        capture_output=True, check=False,
    )


def test_real_node_preserves_task_without_os_argv_disclosure(tmp_path):
    task = "--help\n私有任务 'quotes' $(not-a-command)\n".encode()
    result = launch(tmp_path, task)
    assert result.returncode == 0
    assert result.stdout == task
    assert result.stderr == b""


@pytest.mark.parametrize("task", [b"", b" \n", b"bad\0task", b"\xff", b"x" * 262145],
                         ids=["empty", "whitespace", "nul", "invalid-utf8", "over-limit"])
def test_invalid_input_never_imports_launcher(tmp_path, task):
    marker = tmp_path / "launched"
    result = launch(tmp_path, task, "import {writeFileSync} from 'node:fs'; writeFileSync(new URL('./launched', import.meta.url), 'yes');")
    assert result.returncode != 0
    assert not marker.exists()
    assert not result.stdout


def test_loader_error_is_not_reported_as_success_or_leaked(tmp_path):
    result = launch(tmp_path, b"private task", "throw new Error('private task');")
    assert result.returncode != 0
    assert b"private task" not in result.stderr


def test_child_exit_status_is_preserved(tmp_path):
    result = launch(tmp_path, b"task", "process.exitCode = 7;")
    assert result.returncode == 7
