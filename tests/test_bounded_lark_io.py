"""Real local subprocess fixtures only; never invoke the user's lark-cli."""

import os
import subprocess
import sys
import time

import pytest

from k3_support.bounded_cli import OutputLimitError, run
from k3_support.event_diagnostics import Diagnostics, HISTORY_BYTES, LINE_BYTES, SUFFIX
from k3_support.lark import EventConsumer, LarkError, run_json, run_mail_json


def executable(tmp_path, body):
    path = tmp_path / 'fake-lark'
    path.write_text('#!' + sys.executable + '\nimport os,sys,time,signal,json\n' + body)
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize('size', [10*1024*1024, 100_000])
def test_event_stream_is_bounded_during_flood_and_keeps_ready(tmp_path, size):
    long_line = size > 100_000
    body = "sys.stderr.write('[event] ready event_key=im.message.receive_v1\\n');sys.stderr.flush()\n"
    body += f"sys.stderr.buffer.write(b'x'*{size}+b'\\n')\n" if long_line else "sys.stderr.buffer.write(b'warning\\n'*100000)\n"
    body += "sys.stderr.flush()\nprint('{\"message_id\":\"synthetic\"}',flush=True)\n"
    consumer = EventConsumer(executable=executable(tmp_path, body), ready_timeout=2)
    consumer.start()
    try:
        assert next(consumer.events()) == {'message_id': 'synthetic'}
        assert consumer._stderr_done.wait(5)
        stats = consumer.diagnostic_stats
        assert stats['peak_line_bytes'] <= LINE_BYTES
        assert stats['partial_line_bytes'] == 0
        assert stats['history_lines'] <= 200
        assert stats['history_bytes'] <= HISTORY_BYTES
        assert stats['history_bytes'] == sum(len(line.encode()) for line in consumer.warnings)
        assert all(len(line.encode()) <= LINE_BYTES for line in consumer.warnings)
        assert not any('[event] ready' in line for line in consumer.warnings)
        if long_line:
            assert stats['truncated_lines'] == 1
            assert stats['long_line_dropped_bytes'] == size-(LINE_BYTES-len(SUFFIX.encode()))
        else:
            assert stats['history_evicted_lines'] == 100000-200
        copy = consumer.warnings
        copy.clear()
        assert consumer.warnings
    finally:
        consumer.stop(timeout=1)
    assert not consumer._stderr_thread.is_alive()
    assert all(getattr(consumer.process, name).closed for name in ('stdin', 'stdout', 'stderr'))


def test_byte_budget_and_utf8_split_are_enforced_before_history_storage():
    diagnostics = Diagnostics('expected')
    raw = ('中'*1400 + '\n').encode()
    for offset in range(0, len(raw), 7):
        diagnostics.feed(raw[offset:offset+7])
    assert '\ufffd' not in diagnostics.warnings[0]
    assert len(diagnostics.warnings[0].encode()) <= LINE_BYTES
    for _ in range(200):
        diagnostics.feed(b'x'*4096)
        diagnostics.feed(b'\n')
    assert diagnostics.stats['history_bytes'] <= HISTORY_BYTES
    assert diagnostics.stats['history_lines'] < 200
    assert diagnostics.stats['history_evicted_bytes'] > 0


@pytest.mark.parametrize('marker', [
    '[event] ready event_key=wrong\n',
    '[event] ready event_key=im.message.receive_v1-extra\n',
    '[event] ready event_key=im.message.receive_v1',
])
def test_wrong_or_incomplete_ready_cannot_start_consumer(tmp_path, marker):
    path = executable(tmp_path, f'sys.stderr.write({marker!r});sys.stderr.flush()\n')
    consumer = EventConsumer(executable=path, ready_timeout=1)
    started = time.monotonic()
    with pytest.raises(LarkError):
        consumer.start()
    assert time.monotonic()-started < 1
    assert consumer.process.poll() is not None
    assert not consumer._stderr_thread.is_alive()


def test_ready_without_warning_does_not_depend_on_history(tmp_path):
    path = executable(tmp_path, "print('[event] ready event_key=im.message.receive_v1 identity=bot',file=sys.stderr,flush=True)\nprint('{}',flush=True)\n")
    consumer = EventConsumer(executable=path, ready_timeout=1)
    consumer.start()
    assert next(consumer.events()) == {}
    consumer.stop(timeout=1)
    assert consumer.warnings == []


def test_ready_timeout_cleans_up_reader_and_process(tmp_path):
    consumer = EventConsumer(executable=executable(tmp_path, 'time.sleep(10)\n'), ready_timeout=0.1)
    with pytest.raises(LarkError, match='ready marker'):
        consumer.start()
    assert consumer.process.poll() is not None
    assert not consumer._stderr_thread.is_alive()


def test_actual_process_ignoring_term_is_killed_and_all_streams_close(tmp_path):
    path = executable(tmp_path, "signal.signal(signal.SIGTERM,signal.SIG_IGN)\nprint('[event] ready event_key=im.message.receive_v1',file=sys.stderr,flush=True)\ntime.sleep(10)\n")
    consumer = EventConsumer(executable=path, ready_timeout=1)
    consumer.start()
    with pytest.raises(LarkError, match='was killed'):
        consumer.stop(timeout=0.05)
    assert consumer.process.returncode == -9
    assert not consumer._stderr_thread.is_alive()
    assert consumer.process.stderr.closed


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_finite_capture_aborts_over_budget_and_reaps_process(tmp_path, stream):
    marker = tmp_path / 'child.pid'
    path = executable(tmp_path, f"open({str(marker)!r},'w').write(str(os.getpid()))\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nsys.{stream}.buffer.write(b'x'*10000);sys.{stream}.flush()\ntime.sleep(10)\n")
    with pytest.raises(OutputLimitError, match=stream):
        run([path], timeout=2, env=os.environ.copy(), stdout_limit=32, stderr_limit=32)
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


@pytest.mark.parametrize('runner', [run_json, run_mail_json])
def test_public_cli_output_limit_is_unknown_not_truncated_success(tmp_path, runner):
    path = executable(tmp_path, "print('{\"ok\":true,\"data\":{}}',flush=True)\nsys.stderr.buffer.write(b'x'*(1024*1024));sys.stderr.flush()\n")
    with pytest.raises(LarkError) as caught:
        runner(['test', '--as', 'user'], timeout=2, executable=path)
    assert caught.value.error_type == 'output_limit'
    assert caught.value.subtype == 'remote_result_unknown'


def test_finite_timeout_remains_bounded_and_has_no_output_retry(tmp_path):
    path = executable(tmp_path, 'time.sleep(10)\n')
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run([path], timeout=0.05, env=os.environ.copy())
    assert time.monotonic()-started < 2


def test_large_startup_noise_does_not_evict_ready_handshake(tmp_path):
    path = executable(tmp_path, "sys.stderr.buffer.write(b'warning\\n'*100000)\nprint('[event] ready event_key=im.message.receive_v1',file=sys.stderr,flush=True)\nprint('{}',flush=True)\n")
    consumer = EventConsumer(executable=path, ready_timeout=3)
    consumer.start()
    assert next(consumer.events()) == {}
    consumer.stop(timeout=1)
    assert len(consumer.warnings) == 200
    assert consumer.diagnostic_stats['history_evicted_lines'] == 99800


def test_invalid_utf8_stdout_never_becomes_a_successful_json_payload(tmp_path):
    path = executable(tmp_path, "sys.stdout.buffer.write(b'{\"ok\":true,\"data\":\"\\xff\"}')\n")
    with pytest.raises(LarkError, match='UTF-8') as caught:
        run_json(['test', '--as', 'user'], executable=path)
    assert caught.value.subtype == 'remote_result_unknown'
