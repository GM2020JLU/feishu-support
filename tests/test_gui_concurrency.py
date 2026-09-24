import http.client
import json
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_gui import LOCAL_CONNECT, console as console, login

from k3_support.gui import Console, GuiError, make_server


def wait_for(predicate):
    deadline = time.monotonic()+3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail('concurrency fixture did not reach expected state')


@pytest.mark.parametrize('failure', ['thread_start', 'handler'])
def test_worker_failure_returns_admission_permit(config, monkeypatch, failure):
    server = make_server(config, 'a'*64, port=0, max_workers=1)
    closed, handled = [], []
    request = object()
    monkeypatch.setattr(server, 'shutdown_request', closed.append)
    def handle(*args):
        handled.append(args)
        raise RuntimeError('synthetic handler failure')
    monkeypatch.setattr(server, 'finish_request', handle)
    class InlineThread:
        def __init__(self, *, target, args, **kwargs):
            self.target, self.args = target, args
        def start(self):
            if failure == 'thread_start':
                raise RuntimeError('synthetic thread creation failure')
            self.target(*self.args)
    monkeypatch.setattr(threading, 'Thread', InlineThread)
    try:
        for _ in range(3):
            server.process_request(request, ('127.0.0.1', 1))
            assert server.active_workers == 0
            assert not server._connections
        assert len(closed) == 3
        assert len(handled) == (3 if failure == 'handler' else 0)
        assert server.peak_workers == 1
        assert server.rejected_connections == 0
        assert server._slots.acquire(blocking=False)
        assert not server._slots.acquire(blocking=False)
        server._slots.release()
    finally:
        server.server_close()


def test_idle_connections_do_not_block_status_and_thread_count_is_bounded(config, conn, monkeypatch):
    server = make_server(config, 'a'*64, port=0, max_workers=2)
    port = server.server_port
    def local_only(sock, address):
        assert address == ('127.0.0.1', port)
        return LOCAL_CONNECT(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_only)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = server.console.login('a'*64)
    idle = []
    def status():
        client = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        try:
            client.request('GET', '/api/status', headers={'Cookie': 'feishu_console='+token})
            response = client.getresponse()
            response.read()
            return response.status
        finally:
            client.close()
    try:
        idle.append(socket.create_connection(('127.0.0.1', port)))
        wait_for(lambda: server.active_workers == 1)
        assert status() == 200
        wait_for(lambda: server.active_workers == 1)
        idle.append(socket.create_connection(('127.0.0.1', port)))
        wait_for(lambda: server.active_workers == 2)
        started = time.monotonic()
        with pytest.raises((OSError, http.client.HTTPException)):
            status()
        assert time.monotonic()-started < 2
        assert server.peak_workers == 2 and server.rejected_connections >= 1
        idle.pop().close()
        wait_for(lambda: server.active_workers == 1)
        assert status() == 200
    finally:
        for connection in idle:
            connection.close()
        server.shutdown()
        server.server_close()
        thread.join(3)
    wait_for(lambda: server.active_workers == 0)


def test_slow_archive_with_writer_reservation_does_not_block_readonly_status(console, monkeypatch):
    from k3_support import replay_archive_control
    from k3_support.db import transaction
    request, _ = console
    cookie, csrf = login(request)
    auth = dict(cookie=cookie, csrf=csrf)
    entered, release = threading.Event(), threading.Event()
    def capture(conn, cfg, **values):
        with transaction(conn):
            entered.set()
            assert release.wait(5)
        return {'synthetic': True}
    monkeypatch.setattr(replay_archive_control, 'capture_current', capture)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(request, '/api/replay-archive-capture',
            dict(name='synthetic', event={}, proposal={}, confirmed=True, writers_quiesced=True), **auth)
        try:
            assert entered.wait(2)
            assert request('/api/status', **auth)[0] == 200
        finally:
            release.set()
        assert pending.result(timeout=3)[0] == 200


def test_duplicate_action_and_logout_are_ordered_at_reservation(config, monkeypatch):
    from k3_support import gui
    app = Console(config, 'a'*64)
    token = app.login('a'*64)
    session = app.session(token)
    callback = 'fsc:o:synthetic-panel'
    session['panel'] = {'id': 'synthetic-panel', 'prompt': 'synthetic', 'buttons': [callback]}
    entered, release = threading.Event(), threading.Event()
    calls = []
    def execute(*args, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return {'buttons': [], 'accepted': True}
    monkeypatch.setattr(gui, 'execute_global_callback', execute)
    ident = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(app.action, None, session, callback, ident)
        try:
            assert entered.wait(2)
            with pytest.raises(GuiError, match='未确认'):
                app.action(None, session, callback, ident)
            with pytest.raises(GuiError):
                app.action(None, session, 'different', ident)
            app.logout(token)
            with pytest.raises(PermissionError):
                app.action(None, session, callback, str(uuid.uuid4()))
        finally:
            release.set()
        assert pending.result(timeout=3)['accepted']
    assert len(calls) == 1


def test_archive_writer_conflict_returns_busy_without_success(console, monkeypatch):
    from k3_support import gui, replay_archive_control
    from k3_support.db import transaction
    request, _ = console
    cookie, csrf = login(request)
    auth = dict(cookie=cookie, csrf=csrf)
    code, _, panel = request('/api/panel', {}, **auth)
    assert code == 200
    original_connect = gui.connect
    def short_busy_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.execute('PRAGMA busy_timeout=100')
        return connection
    monkeypatch.setattr(gui, 'connect', short_busy_connect)
    entered, release = threading.Event(), threading.Event()
    def capture(connection, cfg, **values):
        with transaction(connection):
            entered.set()
            assert release.wait(5)
        return {'synthetic': True}
    monkeypatch.setattr(replay_archive_control, 'capture_current', capture)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(request, '/api/replay-archive-capture',
            dict(name='synthetic', event={}, proposal={}, confirmed=True, writers_quiesced=True), **auth)
        try:
            assert entered.wait(2)
            started = time.monotonic()
            code, _, result = request('/api/action',
                {'callback': f"fsc:o:{panel['panel_id']}"}, **auth)
            assert code == 503
            assert '未确认操作成功' in result['error']
            assert time.monotonic()-started < 2
            assert request('/api/status', **auth)[0] == 200
        finally:
            release.set()
        assert pending.result(timeout=3)[0] == 200


def test_old_panel_completion_cannot_replace_new_display(config, monkeypatch):
    from k3_support import gui
    app = Console(config, 'a'*64)
    session = app.session(app.login('a'*64))
    entered, release = threading.Event(), threading.Event()
    def issue(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
            ident = 'old'
        else:
            ident = 'new'
        return {'panel_id': ident, 'buttons': []}
    monkeypatch.setattr(gui, 'issue_global_panel', issue)
    monkeypatch.setattr(gui, 'bind_global_panel', lambda *a, **k: None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(app.panel, None, session)
        try:
            assert entered.wait(2)
            assert app.panel(None, session)['panel_id'] == 'new'
        finally:
            release.set()
        with pytest.raises(GuiError, match='较新的请求'):
            pending.result(timeout=3)
    assert session['panel']['id'] == 'new'


def test_action_receipt_survives_panel_refresh_and_lost_response(config, monkeypatch):
    from k3_support import gui
    app = Console(config, 'a'*64)
    session = app.session(app.login('a'*64))
    callback = 'fsc:o:old'
    session['panel'] = {'id': 'old', 'prompt': 'old', 'buttons': [callback]}
    entered, release = threading.Event(), threading.Event()
    calls = []
    def execute(*args, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return {'buttons': [], 'accepted': True}
    monkeypatch.setattr(gui, 'execute_global_callback', execute)
    monkeypatch.setattr(gui, 'issue_global_panel', lambda *a, **k: {'panel_id': 'new', 'buttons': []})
    monkeypatch.setattr(gui, 'bind_global_panel', lambda *a, **k: None)
    ident = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(app.action, None, session, callback, ident)
        try:
            assert entered.wait(2)
            app.panel(None, session)
            with pytest.raises(GuiError, match='未确认'):
                app.action(None, session, callback, ident)
        finally:
            release.set()
        pending.result(timeout=3)  # Discard the first response, as a disconnected client would.
    assert session['panel']['id'] == 'new'
    assert app.action(None, session, callback, ident)['accepted']
    with pytest.raises(GuiError, match='其他动作'):
        app.action(None, session, 'fsc:o:new', ident)
    assert len(calls) == 1
