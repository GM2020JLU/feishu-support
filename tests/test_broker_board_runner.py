import json
import threading
from uuid import uuid4

import pytest
from test_broker_board import (
    board_request as board_request,  # noqa: PLC0414 - pytest fixture re-export
)
from test_broker_claim import NOW, UID

from k3_support.board_operation_guard import hold
from k3_support.broker_board import submit
from k3_support.broker_board_runner import run_one


@pytest.mark.parametrize("valid", [True, False])
def test_serial_success_requires_fresh_structured_match(conn, board_request, valid):
    cfg, request, reader = board_request
    request["params"]["action"] = {"type": "serial_wait", "regex": "ROM: usb download handler", "timeout": 5}
    submit(conn, cfg, request, peer_uid=UID, contract_reader=reader, now=NOW)
    conn.execute("UPDATE broker_grants SET created_at='2020-01-01T00:00:00+00:00',expires_at='2099-01-01T00:00:00+00:00'")
    conn.execute("UPDATE jobs SET lease_expires_at='2099-01-01T00:00:00+00:00'")
    output = json.dumps({"ok": True, "fresh": valid, "matched": True,
                         "output": "ROM: usb download handler", "rx_seq_start": 42})
    transport = lambda **kw: {"exit_code": 0, "stdout": output, "stderr": ""}
    if valid:
        assert run_one(conn, cfg, contract_reader=reader, transport=transport)["state"] == "succeeded"
    else:
        with pytest.raises(ValueError, match="fresh serial"):
            run_one(conn, cfg, contract_reader=reader, transport=transport)
        assert conn.execute("SELECT state FROM broker_board_actions").fetchone()[0] == "unknown"
        assert conn.execute("SELECT count(*) FROM broker_board_results").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM locks WHERE lock_key='board1'").fetchone()[0] == 1


@pytest.fixture
def queued_board(conn, board_request):
    cfg, request, reader = board_request
    submit(conn, cfg, request, peer_uid=UID, contract_reader=reader, now=NOW)
    conn.execute("UPDATE broker_grants SET created_at='2020-01-01T00:00:00+00:00',expires_at='2099-01-01T00:00:00+00:00'")
    conn.execute("UPDATE jobs SET lease_expires_at='2099-01-01T00:00:00+00:00'")
    return cfg, request, reader


def test_fresh_banner_flows_from_consumer_to_case_detail_without_resolution(conn, board_request):
    from k3_support.board_test_evidence import items
    from k3_support.case_detail import case_detail

    cfg, request, reader = board_request
    request['params']['action'] = {'type': 'serial_wait', 'regex': 'U-Boot', 'timeout': 5}
    submit(conn, cfg, request, peer_uid=UID, contract_reader=reader, now=NOW)
    conn.execute("UPDATE broker_grants SET created_at='2020-01-01T00:00:00+00:00',expires_at='2099-01-01T00:00:00+00:00'")
    conn.execute("UPDATE jobs SET lease_expires_at='2099-01-01T00:00:00+00:00'")
    case, state = conn.execute('SELECT case_id,state FROM cases').fetchone()
    calls = []
    def transport(**kwargs):
        calls.append(kwargs['argv'])
        kwargs['heartbeat']()
        return {'exit_code': 0, 'stderr': '', 'stdout': json.dumps({
            'ok': True, 'fresh': True, 'matched': True, 'rx_seq_start': 17,
            'output': 'U-Boot 2024.01 private-fixture-version\n'})}
    assert run_one(conn, cfg, contract_reader=reader, transport=transport)['state'] == 'succeeded'
    assert len(calls) == 1
    observed = items(conn, case_id=case, lifecycle_round=1)[0]
    assert observed['boot_markers'][0]['marker'] == 'u-boot'
    assert not observed['repair_verified'] and not observed['environment_verified']
    first = case_detail(conn, case_id=case)['preview']
    text = '\n'.join(case_detail(conn, case_id=case, page=i,
        expected_digest=first['content_digest'])['preview']['plain_text']
        for i in range(1, first['page_count'] + 1))
    assert '历史串口标识：u-boot' in text and '不证明当前启动状态' in text
    assert 'private-fixture-version' not in text
    assert conn.execute('SELECT state FROM cases WHERE case_id=?', (case,)).fetchone()[0] == state
    assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0


@pytest.mark.parametrize("code,state", [(0, "succeeded"), (1, "failed"), (-9, "unknown"), (124, "unknown"), (255, "unknown")])
def test_board_consumer_persists_outputs_but_never_claims_cleanup(conn, queued_board, code, state):
    cfg, _, reader = queued_board
    def transport(**kw):
        assert not conn.in_transaction
        assert conn.execute("SELECT state FROM broker_board_actions").fetchone()[0] == "running"
        kw["heartbeat"]()
        return {"exit_code": code, "stdout": "synthetic", "stderr": "diagnostic"}
    result = run_one(conn, cfg, contract_reader=reader, transport=transport)
    assert result["state"] == state and not result["board_cleanup_verified"]
    assert conn.execute("SELECT exit_code FROM broker_board_results").fetchone()[0] == code
    from k3_support.board_test_evidence import items
    case = conn.execute("SELECT case_id FROM jobs").fetchone()[0]
    visible = items(conn, case_id=case, lifecycle_round=1)
    assert len(visible) == 1
    assert not visible[0]["repair_verified"] and not visible[0]["environment_verified"]
    assert "synthetic" not in str(visible) and "diagnostic" not in str(visible)
    assert run_one(conn, cfg, contract_reader=reader, transport=lambda **kw: pytest.fail("no repeat"))["state"] == ("occupied" if state == "unknown" else "idle")
    assert conn.execute("SELECT count(*) FROM locks WHERE lock_key='board1'").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["revoke", "stop", "transport", "invalid"])
def test_board_consumer_uncertainty_retains_occupancy(conn, queued_board, failure):
    cfg, _, reader = queued_board
    stopped = threading.Event()
    def transport(**kw):
        if failure == "revoke":
            conn.execute("UPDATE broker_grants SET revoked_at='fixture'")
            kw["heartbeat"]()
        if failure == "stop":
            stopped.set()
            kw["heartbeat"]()
        if failure == "transport":
            raise OSError("lost device result")
    with pytest.raises((ValueError, OSError)):
        run_one(conn, cfg, contract_reader=reader, transport=transport, stop_event=stopped)
    assert conn.execute("SELECT state FROM broker_board_actions").fetchone()[0] == "unknown"
    assert conn.execute("SELECT count(*) FROM broker_board_results").fetchone()[0] == 0


def test_busy_board_keeps_same_unexecuted_request(conn, queued_board):
    cfg, _, reader = queued_board
    with hold(cfg):
        assert run_one(conn, cfg, contract_reader=reader, transport=lambda **kw: pytest.fail("device busy"))["state"] == "busy"
    assert conn.execute("SELECT state FROM broker_board_actions").fetchone()[0] == "queued"
    assert conn.execute("SELECT count(*) FROM action_ledger").fetchone()[0] == 0


def test_new_request_for_same_action_does_not_reuse_old_device_output(conn, queued_board):
    cfg, request, reader = queued_board
    calls = []
    def transport(**kw):
        calls.append(kw["argv"])
        return {"exit_code": 0, "stdout": str(len(calls)), "stderr": ""}
    run_one(conn, cfg, contract_reader=reader, transport=transport)
    request["request_id"] = str(uuid4())
    submit(conn, cfg, request, peer_uid=UID, contract_reader=reader, now=NOW)
    run_one(conn, cfg, contract_reader=reader, transport=transport)
    assert len(calls) == 2
    assert conn.execute("SELECT count(*) FROM action_ledger").fetchone()[0] == 2


def test_live_authority_revocation_stops_real_synthetic_child(conn, queued_board, tmp_path, monkeypatch):
    import os
    import select
    import signal
    import sqlite3
    import sys
    import time

    from k3_support import broker_board_runner as module
    cfg, _, reader = queued_board
    marker = tmp_path / "synthetic-child.pid"
    code = f"import os,time; open({str(marker)!r},'w').write(str(os.getpid())); time.sleep(60)"
    monkeypatch.setattr(module.BoardExecutor, "_argv", lambda self, action: ([sys.executable, "-I", "-S", "-c", code], 10))
    fds = []
    errors = []
    def revoke():
        try:
            deadline = time.monotonic()+5
            pid_text = ""
            while time.monotonic() < deadline:
                try:
                    pid_text = marker.read_text()
                except FileNotFoundError:
                    pass
                if pid_text.isdigit() and int(pid_text) > 0:
                    break
                time.sleep(.01)
            if not pid_text.isdigit() or int(pid_text) <= 0:
                raise ValueError("synthetic child readiness unavailable")
            fds.append(os.pidfd_open(int(pid_text)))
            db = sqlite3.connect(cfg.database_path, isolation_level=None, timeout=5)
            try:
                db.execute("UPDATE broker_grants SET revoked_at='fixture'")
            finally:
                db.close()
        except Exception as error:  # noqa: BLE001 - relay worker-thread failures to assertions
            errors.append(error)
    revoker = threading.Thread(target=revoke)
    revoker.start()
    try:
        with pytest.raises(ValueError, match="authority"):
            run_one(conn, cfg, contract_reader=reader)
        revoker.join(timeout=6)
        assert not revoker.is_alive() and not errors
        assert conn.execute("SELECT revoked_at FROM broker_grants").fetchone()[0] == "fixture"
        assert len(fds) == 1 and select.select(fds, [], [], 0)[0] == fds
        assert conn.execute("SELECT state FROM broker_board_actions").fetchone()[0] == "unknown"
    finally:
        revoker.join(timeout=6)
        for fd in fds:
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(fd)
