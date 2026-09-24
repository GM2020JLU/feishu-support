import json
import stat
import os

import pytest
from test_replay_debug import capture as fixture
from test_review import remote_runner

from k3_support import debug_transcript_capture as capture
from k3_support.replay_transcript import VerificationTranscript


def test_capture_only_existing_broker_result_and_preserves_source(conn, config):
    cfg, case, job = fixture(conn, config)
    before = conn.serialize()
    result = capture.capture(cfg, job_id=job, confirm_remote_verification=True, runner=remote_runner())
    assert result['job_id'] == job and result['transcript']
    assert VerificationTranscript(result['transcript']).status()['consumed'] == 0
    assert conn.serialize() == before


def test_capture_requires_explicit_confirmation(config):
    with pytest.raises(ValueError, match='confirmation'):
        capture.capture(config, job_id='job', confirm_remote_verification=False,
                        runner=lambda *a: pytest.fail('remote'))


def test_cli_private_bundle_is_exclusive_and_no_retry(conn, config, tmp_path, monkeypatch):
    cfg, case, job = fixture(conn, config)
    target = tmp_path / 'capture.json'
    tmp_path.chmod(0o700)
    monkeypatch.setattr(capture, 'load_config', lambda _: cfg)
    monkeypatch.setattr(capture, 'run_process', remote_runner())
    args = ['--config', 'fixture', '--job-id', job, '--output', str(target), '--confirm-remote-verification']
    assert capture.main(args) == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text())['job_id'] == job
    monkeypatch.setattr(capture, 'run_process', lambda *a: pytest.fail('repeated remote call'))
    assert capture.main(args) == 1


def test_cli_failure_leaves_reserved_file_without_retry(conn, config, tmp_path, monkeypatch):
    cfg, case, job = fixture(conn, config)
    tmp_path.chmod(0o700)
    target = tmp_path / 'failed.json'
    monkeypatch.setattr(capture, 'load_config', lambda _: cfg)
    calls = []
    def unavailable(*args):
        calls.append(1)
        raise RuntimeError('unavailable')
    monkeypatch.setattr(capture, 'run_process', unavailable)
    args = ['--config', 'fixture', '--job-id', job, '--output', str(target), '--confirm-remote-verification']
    assert capture.main(args) == 1
    assert calls == [1] and target.read_bytes() == b''
    assert capture.main(args) == 1
    assert calls == [1]


@pytest.mark.parametrize('kind', ['directory_symlink', 'file_symlink', 'public_directory'])
def test_unsafe_output_is_rejected_without_changing_target(tmp_path, kind):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    original = tmp_path / 'original'
    original.write_text('keep')
    target = private / 'capture.json'
    if kind == 'directory_symlink':
        link = tmp_path / 'link'
        link.symlink_to(private, target_is_directory=True)
        target = link / 'capture.json'
    elif kind == 'file_symlink':
        target.symlink_to(original)
    else:
        private.chmod(0o755)
    with pytest.raises((ValueError, OSError)):
        with capture.private_output(target):
            pytest.fail('unsafe destination accepted')
    assert original.read_text() == 'keep'


def test_directory_replacement_does_not_redirect_write(tmp_path, monkeypatch):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    moved = tmp_path / 'moved'
    other = tmp_path / 'other'
    other.mkdir(mode=0o700)
    original_open = os.open
    def replace(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            private.rename(moved)
            private.symlink_to(other, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(capture.os, 'open', replace)
    with pytest.raises(ValueError, match='moved'):
        with capture.private_output(private / 'capture.json') as output:
            output.write('captured')
    assert not (other / 'capture.json').exists()
    assert (moved / 'capture.json').read_text() == 'captured'
