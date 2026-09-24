from __future__ import annotations

import http.client
import json
import socket
import threading
import uuid

import pytest

from k3_support.gui import make_server
from k3_support.runtime_control import bind_global_panel, issue_global_panel

LOCAL_CONNECT = socket.socket.connect


def test_archive_capture_http_requires_auth_and_fixed_payload(console, config, monkeypatch):
    from k3_support import replay_archive_control
    request, _ = console
    payload = dict(name='one', event={}, proposal={}, confirmed=True, writers_quiesced=True)
    assert request('/api/replay-archive-capture', payload)[0] == 403
    cookie, csrf = login(request)
    assert request('/api/replay-archive-capture', payload, cookie=cookie)[0] == 403
    auth = dict(cookie=cookie, csrf=csrf)
    calls = []
    def capture(conn, cfg, **values):
        calls.append((cfg, values))
        return dict(name='one', manifest_digest='a'*64, release_authorized=False)
    monkeypatch.setattr(replay_archive_control, 'capture_current', capture)
    assert request('/api/replay-archive-capture', payload | {'output':'/tmp/x'}, **auth)[0] == 409
    assert not calls
    code, _, result = request('/api/replay-archive-capture', payload, **auth)
    assert code == 200 and result['release_authorized'] is False
    assert calls == [(config, payload)]
    def fail(*args, **kwargs):
        raise OSError('PRIVATE /secret')
    monkeypatch.setattr(replay_archive_control, 'capture_current', fail)
    code, _, result = request('/api/replay-archive-capture', payload, **auth)
    assert code == 409 and 'PRIVATE' not in str(result) and '/secret' not in str(result)


def test_archive_run_requires_auth_exact_fields_and_managed_root(console, config, monkeypatch):
    from k3_support import replay_archive_control
    request, _ = console
    payload = {'name':'one','manifest_digest':'a'*64,'confirmed':True}
    assert request('/api/replay-archive-run',payload)[0]==403
    cookie,csrf=login(request)
    assert request('/api/replay-archive-run',payload,cookie=cookie)[0]==403
    auth={'cookie':cookie,'csrf':csrf}
    calls=[]
    def run(root,**kwargs):
        calls.append((root,kwargs))
        return {'fixture':True,'release_authorized':False}
    monkeypatch.setattr(replay_archive_control,'run_selected',run)
    assert request('/api/replay-archive-run',{**payload,'root':'/etc'},**auth)[0]==409
    assert not calls
    code,_,result=request('/api/replay-archive-run',payload,**auth)
    assert code==200 and result['fixture'] and not result['release_authorized']
    assert calls==[(config.data_dir/'replay-archives',payload)]
    def fail(*args,**kwargs):
        raise ValueError('PRIVATE ARCHIVE /private/path')
    monkeypatch.setattr(replay_archive_control,'run_selected',fail)
    code,_,result=request('/api/replay-archive-run',payload,**auth)
    assert code==409 and 'PRIVATE' not in str(result) and '/private/path' not in str(result)


def test_replay_catalog_requires_auth_and_rejects_browser_paths(console, config):
    request, _ = console
    assert request('/api/replay-archives', {})[0] == 403
    cookie, csrf = login(request)
    assert request('/api/replay-archives', {}, cookie=cookie)[0] == 403
    auth = {'cookie': cookie, 'csrf': csrf}
    assert request('/api/replay-archives', {'root': '/etc'}, **auth)[0] == 409
    root = config.data_dir/'replay-archives'
    assert not root.exists()
    code, _, result = request('/api/replay-archives', {}, **auth)
    assert code == 200 and result['items'] == [] and result['read_only']
    assert not root.exists()
    root.mkdir(parents=True)
    (root/'incomplete').mkdir()
    code, _, result = request('/api/replay-archives', {}, **auth)
    assert code == 200 and result['items'][0]['status'] == 'incomplete'
    assert result['items'][0]['replay_ready'] is False


def test_case_inventory_requires_authenticated_csrf_and_returns_no_body(console, conn):
    from k3_support.store import create_case
    request, _ = console
    case, _ = create_case(conn, title='PRIVATE INVENTORY TITLE', case_type='faq', severity='P3', confidence=.9)
    body = {'case_id': case}
    assert request('/api/case-content-inventory', body)[0] == 403
    cookie, csrf = login(request)
    assert request('/api/case-content-inventory', body, cookie=cookie)[0] == 403
    code, _, result = request('/api/case-content-inventory', body, cookie=cookie, csrf=csrf)
    assert code == 200 and result['read_only'] and not result['deletion_allowed']
    assert 'PRIVATE INVENTORY TITLE' not in str(result)
    assert conn.execute('SELECT title FROM cases WHERE case_id=?', (case,)).fetchone()[0] == 'PRIVATE INVENTORY TITLE'


@pytest.mark.parametrize('scope', ['body', 'draft'])
def test_body_retention_policy_http_requires_session_preview_and_confirmation(console, conn, scope):
    request, _ = console
    original_request = request
    def request(path, *args, **kwargs):
        return original_request(path.replace('/api/body-retention-', f'/api/{scope}-retention-'), *args, **kwargs)
    assert request('/api/body-retention-policy', {})[0] == 403
    cookie, csrf = login(request)
    auth = {'cookie': cookie, 'csrf': csrf}
    assert request('/api/body-retention-policy-preview', {'expected_revision': 0}, **auth)[0] == 409
    code, _, draft = request('/api/body-retention-policy-preview', {'expected_revision': 0, 'days': 30}, **auth)
    assert code == 200
    payload = {'draft_id': draft['draft_id'], 'actor_id': 'forged'}
    assert request('/api/body-retention-policy-apply', payload, **auth)[0] == 409
    payload['confirm_policy_change'] = True
    assert request('/api/body-retention-policy-apply', payload, cookie=cookie)[0] == 403
    assert request('/api/body-retention-policy-apply', payload, **auth)[0] == 200
    code, _, state = request('/api/body-retention-policy', {}, **auth)
    assert code == 200 and state['days'] == 30
    assert conn.execute(f'SELECT actor_id FROM {scope}_retention_policy_history').fetchone()[0] != 'forged'
    assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 0


def test_retention_http_cannot_apply_preview_to_other_policy(console, conn):
    request, _ = console
    cookie, csrf = login(request)
    auth = {'cookie': cookie, 'csrf': csrf}
    code, _, draft = request('/api/body-retention-policy-preview', {'expected_revision': 0, 'days': 30}, **auth)
    assert code == 200
    assert request('/api/draft-retention-policy-apply', {'draft_id': draft['draft_id'], 'confirm_policy_change': True}, **auth)[0] == 409
    assert conn.execute('SELECT count(*) FROM draft_retention_settings').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM body_retention_settings').fetchone()[0] == 0


def test_preview_invalid_time_returns_explicit_not_started(console):
    request, app = console
    app.previews.runner = lambda *args, **kwargs: pytest.fail('must not invoke model')
    cookie, csrf = login(request)
    code, _, result = request('/api/model-preview-start',
        {'request_id': str(uuid.uuid4()), 'event': {}, 'confirm_model_call': True,
         'assumptions': {'observed_at': '2026-09-09T12:00:00'}}, cookie=cookie, csrf=csrf)
    assert code == 200 and result['state'] == 'rejected'
    assert result['execution_state'] == 'not_started'
    assert not app.previews.tasks


def test_running_model_preview_does_not_block_console_http(console):
    from test_gui_inference import finished
    request, app = console
    entered, release = threading.Event(), threading.Event()

    def runner(config, event):
        entered.set()
        if not release.wait(5):
            raise TimeoutError('synthetic held model')
        return {'scope': 'routing_only'}

    app.previews.runner = runner
    cookie, csrf = login(request)
    payload = {'request_id': str(uuid.uuid4()), 'event': {}, 'confirm_model_call': True}
    try:
        assert request('/api/model-preview-start', payload, cookie=cookie, csrf=csrf)[0] == 200
        assert entered.wait(1)
        code, _, status = request('/api/model-preview-status', {'request_id': payload['request_id']}, cookie=cookie, csrf=csrf)
        assert code == 200 and status['state'] == 'running'
        assert request('/api/session', cookie=cookie)[0] == 200
        assert not release.is_set()
    finally:
        release.set()
    token = next(iter(app.sessions))
    assert finished(app.previews, token, payload['request_id'])['state'] == 'completed'


def test_model_preview_http_authenticated_and_session_private(console, conn):
    from test_gui_inference import finished
    request, app = console
    app.previews.runner = lambda config, event: {'scope': 'routing_only', 'external_consumers': False}
    value = {'request_id': str(uuid.uuid4()), 'event': {'payload': {'content': 'question'}}, 'confirm_model_call': True}
    before = conn.serialize()
    assert request('/api/model-preview-start', value)[0] == 403
    cookie, csrf = login(request)
    assert request('/api/model-preview-start', value, cookie=cookie)[0] == 403
    code, _, result = request('/api/model-preview-start', value, cookie=cookie, csrf=csrf)
    assert code == 200
    token = next(key for key in app.sessions)
    finished(app.previews, token, value['request_id'])
    code, _, result = request('/api/model-preview-status', {'request_id': value['request_id']}, cookie=cookie, csrf=csrf)
    assert code == 200 and result['state'] == 'completed'
    second_cookie, second_csrf = login(request)
    assert request('/api/model-preview-status', {'request_id': value['request_id']}, cookie=second_cookie, csrf=second_csrf)[0] == 409
    assert conn.serialize() == before


def test_draft_clear_http_requires_auth_confirmation_and_server_actor(console, conn, config):
    from test_draft_retention import seed

    identifier = seed(conn)
    request, _ = console
    payload = {'candidate_id': identifier, 'days': 30}
    assert request('/api/draft-retention-preview', payload)[0] == 403
    cookie, csrf = login(request)
    auth = {'cookie': cookie, 'csrf': csrf}
    before = conn.serialize()
    assert request('/api/draft-retention-preview', payload, cookie=cookie)[0] == 403
    code, _, value = request('/api/draft-retention-preview', payload, **auth)
    assert code == 200 and value['eligible'] and conn.serialize() == before
    payload.update(preview_digest=value['row_digest'], actor_id='forged')
    assert request('/api/draft-retention-clear', payload)[0] == 403
    assert request('/api/draft-retention-clear', payload, cookie=cookie)[0] == 403
    assert request('/api/draft-retention-clear', payload, **auth)[0] == 409
    assert conn.serialize() == before
    payload['confirm_logical_delete'] = True
    code, _, result = request('/api/draft-retention-clear', payload, **auth)
    assert code == 200 and not result['secure_erasure']
    reason = json.loads(conn.execute('SELECT reason FROM retention_tombstones').fetchone()[0])
    assert reason['actor_id'] == config.telegram_control_user_id
    assert request('/api/draft-retention-clear', payload, **auth)[0] == 409


def test_draft_clear_http_rechecks_reference_added_after_preview(console, conn):
    from test_draft_retention import seed

    identifier = seed(conn)
    request, _ = console
    cookie, csrf = login(request)
    auth = {'cookie': cookie, 'csrf': csrf}
    payload = {'candidate_id': identifier, 'days': 30}
    code, _, value = request('/api/draft-retention-preview', payload, **auth)
    assert code == 200 and value['eligible']
    conn.execute('CREATE TABLE synthetic_late_reference(payload_json TEXT)')
    conn.execute('INSERT INTO synthetic_late_reference VALUES(json_object(?,?))', ('draft', identifier))
    before = conn.serialize()
    payload.update(preview_digest=value['row_digest'], confirm_logical_delete=True)
    assert request('/api/draft-retention-clear', payload, **auth)[0] == 409
    assert conn.serialize() == before


@pytest.mark.parametrize('identifier', [None, [], {}, 42, 'bad-id'])
def test_draft_retention_http_rejects_bad_ids_without_writes(console, conn, identifier):
    request, _ = console
    cookie, csrf = login(request)
    before = conn.serialize()
    for endpoint in ('preview', 'clear'):
        payload = {'candidate_id': identifier, 'days': 30, 'confirm_logical_delete': True,
                   'preview_digest': 'a' * 64}
        assert request('/api/draft-retention-' + endpoint, payload, cookie=cookie, csrf=csrf)[0] == 409
    assert conn.serialize() == before


def test_backup_inspection_http_is_authenticated_and_readonly(console, conn, config, monkeypatch):
    from test_backup_prune_inspect import uncertain

    root, receipt = uncertain(config, monkeypatch)
    request, _ = console
    payload = {'receipt_id': receipt['receipt_id']}
    assert request('/api/backup-retention-inspect', payload)[0] == 403
    cookie, csrf = login(request)
    assert request('/api/backup-retention-inspect', payload, cookie=cookie)[0] == 403
    before = conn.serialize()
    original = (root / '.retention-audit' / (receipt['receipt_id'] + '.json')).read_bytes()
    code, _, result = request('/api/backup-retention-inspect', payload, cookie=cookie, csrf=csrf)
    assert code == 200 and result['target_state'] == 'same_version'
    assert result['read_only'] and not result['retry_authorized']
    code, _, listing = request('/api/backup-retention-history', {}, cookie=cookie, csrf=csrf)
    assert code == 200 and len(listing['items']) == 1
    assert conn.serialize() == before
    assert (root / '.retention-audit' / (receipt['receipt_id'] + '.json')).read_bytes() == original


@pytest.mark.parametrize('scoped', [False, True])
def test_attachment_save_auth_identity_and_durable_readback(console, conn, config, scoped):
    from test_knowledge_authoring import fields

    from k3_support.docling_draft import build_draft

    request, _ = console
    value = fields()
    if scoped:
        value['authored_scope'] = {'product': 'K3', 'component': 'u-boot', 'software_version': 'commit-1'}
    payload = {'fields': value, 'expected_digest': build_draft(**value)['revision_digest'], 'actor_id': 'forged'}
    assert request('/api/attachment-draft-save', payload)[0] == 403
    cookie, csrf = login(request)
    code, _, preview = request('/api/attachment-draft', value, cookie=cookie, csrf=csrf)
    assert code == 200 and preview['revision_digest'] == payload['expected_digest']
    assert request('/api/attachment-draft-save', payload, cookie=cookie)[0] == 403
    code, _, saved = request('/api/attachment-draft-save', payload, cookie=cookie, csrf=csrf)
    assert code == 200 and saved['status'] == 'captured'
    code, _, repeated = request('/api/attachment-draft-save', payload, cookie=cookie, csrf=csrf)
    assert code == 200 and repeated['replayed']
    assert request('/api/knowledge-authoring-detail', {'candidate_id': saved['candidate_id']})[0] == 403
    code, _, loaded = request('/api/knowledge-authoring-detail', {'candidate_id': saved['candidate_id']}, cookie=cookie, csrf=csrf)
    assert code == 200 and loaded['saved_by'] == config.telegram_control_user_id
    assert loaded['metadata']['review'] is None and not loaded['automatic_reply_eligible']
    assert loaded['metadata']['scope']['product'] == ('K3' if scoped else 'unresolved')
    assert loaded['metadata']['scope']['basis'] == 'unresolved'
    code, _, listed = request('/api/knowledge-authoring-list', {}, cookie=cookie, csrf=csrf)
    assert code == 200 and len(listed['items']) == 1
    payload['fields']['answer'] = 'changed after preview'
    assert request('/api/attachment-draft-save', payload, cookie=cookie, csrf=csrf)[0] == 409
    assert conn.execute('SELECT count(*) FROM knowledge_authoring_drafts').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM knowledge_entries').fetchone()[0] == 0


@pytest.mark.parametrize('column', ['markdown', 'material_json'])
def test_corrupt_attachment_detail_and_retry_fail_without_content_leak(console, conn, column):
    from test_knowledge_authoring import fields
    from k3_support.docling_draft import build_draft

    request, _ = console
    cookie, csrf = login(request)
    value = fields()
    payload = {'fields': value, 'expected_digest': build_draft(**value)['revision_digest']}
    code, _, saved = request('/api/attachment-draft-save', payload, cookie=cookie, csrf=csrf)
    assert code == 200
    marker = 'private-corrupted-attachment-content'
    replacement = json.dumps(marker) if column == 'material_json' else marker
    conn.execute(f'UPDATE knowledge_authoring_drafts SET {column}=?', (replacement,))
    before = conn.execute('SELECT * FROM knowledge_authoring_drafts').fetchall()
    for endpoint, body in [('/api/knowledge-authoring-detail', {'candidate_id': saved['candidate_id']}),
                           ('/api/attachment-draft-save', payload)]:
        code, _, response = request(endpoint, body, cookie=cookie, csrf=csrf)
        assert code == 409
        assert marker not in str(response)
        assert 'markdown' not in response and 'material' not in response
    assert [tuple(row) for row in conn.execute('SELECT * FROM knowledge_authoring_drafts')] == [tuple(row) for row in before]
    assert conn.execute('SELECT count(*) FROM knowledge_entries').fetchone()[0] == 0


def test_purge_http_requires_auth_confirmation_and_server_actor(console, conn, config):
    from uuid import uuid4
    from test_retention_recheck import candidate
    from k3_support.operations import apply_retention
    request, _ = console
    _, _, candidates = candidate(conn, config)
    apply_retention(conn, config, candidates)
    conn.execute("UPDATE retention_attempts SET updated_at='2020-01-01T00:00:00+00:00'")
    attempt = conn.execute('SELECT attempt_id FROM retention_attempts').fetchone()[0]
    payload = {'attempt_id': attempt, 'days': 30}
    assert request('/api/retention-purge-preview', payload)[0] == 403
    cookie, csrf = login(request)
    assert request('/api/retention-purge-preview', payload, cookie=cookie)[0] == 403
    code, _, shown = request('/api/retention-purge-preview', payload, cookie=cookie, csrf=csrf)
    assert code == 200 and not shown['deletion_authorized']
    prepared = {**payload, 'request_id': str(uuid4()), 'binding_digest': shown['binding_digest'], 'actor_id': 'forged'}
    assert request('/api/retention-purge-prepare', prepared, cookie=cookie, csrf=csrf)[0] == 409
    prepared['confirm_permanent_delete'] = True
    assert request('/api/retention-purge-prepare', prepared, cookie=cookie, csrf=csrf)[0] == 200
    assert conn.execute('SELECT actor_id FROM retention_purge_requests').fetchone()[0] == config.telegram_control_user_id
    target = {'request_id': prepared['request_id']}
    assert request('/api/retention-purge-execute', target)[0] == 403
    assert request('/api/retention-purge-execute', target, cookie=cookie)[0] == 403
    assert request('/api/retention-purge-cancel', target, cookie=cookie, csrf=csrf)[0] == 200
    code, _, result = request('/api/retention-purge-execute', target, cookie=cookie, csrf=csrf)
    assert code == 200 and result['files_deleted'] == 0 and result['state'] == 'cancelled'


def test_attachment_preview_requires_auth_and_does_not_write(console, conn):
    from test_docling_review import evidence

    request, _ = console
    payload = {"evidence": evidence()}
    assert request('/api/attachment-evidence-preview', payload)[0] == 403
    cookie, csrf = login(request)
    assert request('/api/attachment-evidence-preview', payload, cookie=cookie)[0] == 403
    before = conn.serialize()
    code, _, result = request('/api/attachment-evidence-preview', payload, cookie=cookie, csrf=csrf)
    assert code == 200 and result['read_only'] and result['status'] == 'unreviewed'
    assert conn.serialize() == before


def test_attachment_draft_is_unpublished_and_readonly(console, conn):
    from test_docling_review import evidence

    request, _ = console
    payload = {"evidence": evidence(), "title": "fixture", "question": "question",
               "answer": "unverified", "references": ["#/texts/0"], "risk_class": "read_only"}
    assert request('/api/attachment-draft', payload)[0] == 403
    cookie, csrf = login(request)
    assert request('/api/attachment-draft', payload, cookie=cookie)[0] == 403
    before = conn.serialize()
    code, _, result = request('/api/attachment-draft', payload, cookie=cookie, csrf=csrf)
    assert code == 200 and result['read_only'] and result['status'] == 'captured'
    assert 'automatic_reply: false' in result['markdown']
    assert conn.serialize() == before


def test_body_retention_preview_http_is_private_and_readonly(console, conn):
    from k3_support.store import ingest_event

    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='retention-http',
        payload={'content': 'PRIVATE ORIGINAL BODY'}, occurred_at='2025-01-01T00:00:00Z')
    conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
    request, _ = console
    assert request('/api/body-retention-preview', {'days': 30})[0] == 403
    cookie, csrf = login(request)
    assert request('/api/body-retention-preview', {'days': 30}, cookie=cookie)[0] == 403
    before = conn.serialize()
    code, _, result = request('/api/body-retention-preview', {'days': 30}, cookie=cookie, csrf=csrf)
    assert code == 200 and result['read_only']
    assert conn.serialize() == before
    assert 'PRIVATE ORIGINAL BODY' not in str(result)
    item = next(item for item in result['items'] if item['event_pk'] == event)
    assert item['body_state'] == 'stored' and not item['deletion_allowed']
    assert request('/api/body-retention-preview', {'days': True}, cookie=cookie, csrf=csrf)[0] == 409


def test_body_clear_http_requires_confirmation_and_server_actor(console, conn, config):
    from k3_support.body_retention import preview
    from k3_support.store import ingest_event

    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='body-clear-http',
        payload={'content': 'private'}, occurred_at='2025-01-01T00:00:00Z')
    conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
    item = preview(conn, days=30)['items'][0]
    request, _ = console
    payload = {'days':30, 'expected':{event:item['snapshot_digest']}, 'actor':'forged'}
    assert request('/api/body-retention-clear', payload)[0] == 403
    cookie, csrf = login(request)
    assert request('/api/body-retention-clear', payload, cookie=cookie)[0] == 403
    auth = {'cookie':cookie, 'csrf':csrf}
    assert request('/api/body-retention-clear', payload, **auth)[0] == 409
    payload['confirm_database_body_only'] = True
    assert request('/api/body-retention-clear', payload, **auth)[0] == 409
    payload['confirm_error_details'] = True
    code, _, result = request('/api/body-retention-clear', payload, **auth)
    assert code == 200 and result['cleared'] == 1
    assert conn.execute('SELECT actor FROM body_retention_receipts WHERE event_pk=?',(event,)).fetchone()[0] == config.telegram_control_user_id
    assert request('/api/body-retention-clear', payload, **auth)[0] == 409


def test_body_clear_http_rejects_new_reference_after_preview(console, conn):
    from k3_support.store import ingest_event

    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='late-reference-http',
        payload={'content': 'must remain'}, occurred_at='2025-01-01T00:00:00Z')
    conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
    request, _ = console
    cookie, csrf = login(request)
    auth = {'cookie':cookie, 'csrf':csrf}
    code, _, shown = request('/api/body-retention-preview', {'days':30}, **auth)
    assert code == 200
    item = next(item for item in shown['items'] if item['event_pk'] == event)
    conn.execute('CREATE TABLE new_case_reference(source TEXT REFERENCES inbound_events(event_pk))')
    conn.execute('INSERT INTO new_case_reference VALUES(?)', (event,))
    before = conn.serialize()
    code, _, _ = request('/api/body-retention-clear', {'days':30, 'expected':{event:item['snapshot_digest']},
                                                     'confirm_database_body_only':True,'confirm_error_details':True}, **auth)
    assert code == 409
    assert conn.serialize() == before


@pytest.mark.parametrize("source_state", ["bound", "changed", "legacy"])
def test_sent_knowledge_feedback_http_uses_owner_and_exact_receipt(console, conn, config, source_state):
    from test_knowledge_use_preview import seed_sent_reply

    from k3_support.knowledge_runtime import event_input_digest
    from k3_support.store import ingest_event

    case, _, outbox, payload = seed_sent_reply(conn)
    event_pk, _ = ingest_event(conn, source="feishu_user_poll", identity="user",
        external_id="feedback-question", payload={"content": "Pico 风扇怎么调 token=private-value"},
        occurred_at="2026-09-08T00:00:00Z", sender_id="colleague", chat_id="chat")
    payload["knowledge_release"]["source_event_pk"] = event_pk
    if source_state != "legacy":
        event = conn.execute("SELECT * FROM inbound_events WHERE event_pk=?", (event_pk,)).fetchone()
        payload["knowledge_release"]["provenance"]["knowledge_event_digest"] = event_input_digest(event)
    conn.execute("UPDATE outbox SET payload_json=?,source_event_pk=? WHERE outbox_id=?",
                 (json.dumps(payload), event_pk, outbox))
    request, _ = console
    target = {"case_id": case, "use_id": "use-1"}
    assert request("/api/sent-knowledge-preview", target)[0] == 403
    cookie, csrf = login(request)
    auth = {"cookie": cookie, "csrf": csrf}
    status, _, shown = request("/api/sent-knowledge-preview", target, **auth)
    assert status == 200 and shown["version_bound"]
    body = {**target, "content_digest": shown["content_digest"], "verdict": "incorrect",
            "request_id": str(uuid.uuid4()), "actor_id": "forged"}
    assert request("/api/sent-knowledge-feedback", body, cookie=cookie)[0] == 403
    status, _, result = request("/api/sent-knowledge-feedback", body, **auth)
    assert status == 200 and result["created"] and not result["knowledge_changed"]
    assert conn.execute("SELECT actor_id FROM sent_knowledge_feedback").fetchone()[0] == config.telegram_control_user_id
    assert not request("/api/sent-knowledge-feedback", body, **auth)[2]["created"]
    status, _, queue = request("/api/sent-knowledge-pending", {}, **auth)
    assert status == 200 and len(queue["items"]) == 1
    item = queue["items"][0]
    review = {"feedback_id": item["request_id"], "content_digest": item["review_digest"],
              "decision": "needs_revision", "reason": "缺少适用版本", "actor_id": "forged",
              "request_id": str(uuid.uuid4())}
    assert request("/api/sent-knowledge-review", review, cookie=cookie)[0] == 403
    assert request("/api/sent-knowledge-review", review, **auth)[2]["created"]
    assert not request("/api/sent-knowledge-review", review, **auth)[2]["created"]
    assert not request("/api/sent-knowledge-pending", {}, **auth)[2]["items"]
    tracked = request("/api/sent-knowledge-pending", {"state": "needs_revision"}, **auth)[2]["items"]
    assert len(tracked) == 1 and tracked[0]["reason"] == "缺少适用版本"
    assert conn.execute("SELECT actor_id FROM sent_feedback_reviews").fetchone()[0] == config.telegram_control_user_id
    if source_state == "changed":
        conn.execute("UPDATE inbound_events SET payload_json=? WHERE event_pk=?",
                     (json.dumps({"content": "different question"}), event_pk))
    material_target = {"feedback_id": item["request_id"], "actor_id": "forged"}
    assert request("/api/sent-knowledge-material", material_target, cookie=cookie)[0] == 403
    before = conn.serialize()
    status, _, material = request("/api/sent-knowledge-material", material_target, **auth)
    assert status == 200
    assert conn.serialize() == before
    candidate = material["regression_candidate"]
    if source_state == "bound":
        assert candidate["status"] == "candidate"
        assert candidate["review"]["decision"] is None
        assert "private-value" not in json.dumps(candidate)
        assert "original sent answer" not in json.dumps(candidate)
    else:
        assert candidate is None and not material["question_available"]


@pytest.fixture
def console(config, conn, monkeypatch):
    server = make_server(config, "a" * 64, port=0)
    port = server.server_port

    def local_only(sock, address):
        assert address == ("127.0.0.1", port), (
            "GUI test attempted non-fixture transport"
        )
        return LOCAL_CONNECT(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(path, body=None, *, cookie="", csrf="", origin=True, host=None):
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
            "Host": host or f"127.0.0.1:{port}",
        }
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{port}"
        payload = None
        if body is not None:
            body = dict(body)
            if path == "/api/action":
                body.setdefault("request_id", str(uuid.uuid4()))
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body)
        client.request("POST" if body is not None else "GET", path, payload, headers)
        response = client.getresponse()
        content = response.read()
        result = (
            json.loads(content)
            if response.getheader("Content-Type", "").startswith("application/json")
            else content
        )
        output = response.status, dict(response.getheaders()), result
        client.close()
        return output

    try:
        yield request, server.console
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def login(request):
    code, headers, body = request("/api/login", {"key": "a" * 64})
    assert code == 200
    assert (
        "HttpOnly" in headers["Set-Cookie"]
        and "SameSite=Strict" in headers["Set-Cookie"]
    )
    return headers["Set-Cookie"].split(";", 1)[0], body["csrf"]


def test_watch_settings_http_binds_owner_revision_and_csrf(console, conn, config):
    from k3_support.store import create_case

    case_id, _ = create_case(conn, title="synthetic", case_type="bug", severity="P3", confidence=0.8)
    request, _ = console
    target = {"source_kind": "case", "source_key": case_id, "owner_id": "forged"}
    assert request("/api/watch-settings", target)[0] == 403
    cookie, csrf = login(request)
    auth = {"cookie": cookie, "csrf": csrf}
    assert request("/api/watch-settings", target, **auth)[2]["revision"] == 0
    body = {**target, "enabled": True, "expected_revision": 0, "request_id": str(uuid.uuid4())}
    assert request("/api/watch-save", body, cookie=cookie)[0] == 403
    code, _, result = request("/api/watch-save", body, **auth)
    assert code == 200 and result["owner_id"] == config.telegram_control_user_id
    assert request("/api/watch-save", body, **auth)[2] == result
    assert request("/api/watch-save", {**body, "request_id": str(uuid.uuid4())}, **auth)[0] == 409
    assert conn.execute("SELECT count(*) FROM watch_subscription_history").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_watch_seen_http_permissions_and_idempotency(console, conn, config):
    from datetime import UTC, datetime

    from k3_support.store import create_case
    from k3_support.watch_subscriptions import collect_cases, configure, page

    case_id, _ = create_case(conn, title="synthetic", case_type="bug", severity="P3", confidence=0.8)
    ids = {}
    for owner in (config.telegram_control_user_id, "other"):
        configure(conn, owner_id=owner, source_kind="case", source_key=case_id, enabled=True,
                  expected_revision=0, request_id=str(uuid.uuid4()), now=datetime(2020, 1, 1, tzinfo=UTC))
        collect_cases(conn, owner_id=owner)
        ids[owner] = page(conn, owner_id=owner)["items"][0]["action_id"]
    request, _ = console
    body = {"action_id": ids[config.telegram_control_user_id]}
    assert request("/api/watch-seen", body)[0] == 403
    cookie, csrf = login(request)
    auth = {"cookie": cookie, "csrf": csrf}
    assert request("/api/watch-seen", body, cookie=cookie)[0] == 403
    before = list(conn.iterdump())
    assert request("/api/watch-seen", {"action_id": ids["other"], "owner_id": "other"}, **auth)[0] == 409
    assert list(conn.iterdump()) == before
    code, _, result = request("/api/watch-seen", body, **auth)
    assert code == 200 and result["owner_id"] == config.telegram_control_user_id
    assert request("/api/watch-seen", body, **auth)[2] == result
    assert request("/api/watch-list", {}, **auth)[2]["items"] == []
    assert len(page(conn, owner_id="other")["items"]) == 1
    assert conn.execute("SELECT count(*) FROM watch_seen").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_work_hours_editor_http_requires_session_and_explicit_apply(console, conn, config):
    request, _ = console
    assert request("/api/work-hours", {})[0] == 403
    cookie, csrf = login(request)
    auth = {"cookie": cookie, "csrf": csrf}
    current = request("/api/work-hours", {}, **auth)[2]
    body = {"values": {"start":"10:00","end":"19:00"}, "expected_revision":current["revision"]}
    assert request("/api/work-hours-preview", body, cookie=cookie)[0] == 403
    pending = request("/api/work-hours-preview", body, **auth)[2]
    assert config.work_hours == current["values"]
    assert request("/api/work-hours-apply", {"draft_id": pending["draft_id"]}, **auth)[0] == 200
    assert config.work_hours == body["values"]
    assert conn.execute("SELECT actor_id FROM work_hours_history").fetchone()[0] == "owner-user"
    refreshed = request("/api/work-hours", {}, **auth)[2]
    assert refreshed["history"][0]["revision"] == 1
    rollback = request("/api/work-hours-preview", {"expected_revision": 1, "rollback_revision": 1}, **auth)[2]
    assert config.work_hours == body["values"]
    assert request("/api/work-hours-apply", {"draft_id": rollback["draft_id"]}, **auth)[2]["revision"] == 2
    assert config.work_hours == current["values"]


def test_attention_http_binds_operator_and_requires_csrf(console, conn):
    request, _ = console
    assert request("/api/attention-subscriptions", {})[0] == 403
    cookie, csrf = login(request)
    auth = {"cookie": cookie, "csrf": csrf}
    body = {"owner_id": "forged", "category": "upstream", "enabled": True,
            "expected_revision": 0, "request_id": str(uuid.uuid4())}
    assert request("/api/attention-subscription-save", body, cookie=cookie)[0] == 403
    code, _, saved = request("/api/attention-subscription-save", body, **auth)
    assert code == 200 and saved["owner_id"] == "owner-user"
    assert request("/api/attention-subscription-save", body, **auth)[2] == saved
    before = list(conn.iterdump())
    assert request("/api/attention-subscriptions", {}, **auth)[2]["items"][0]["revision"] == 1
    assert request("/api/attention-list", {}, **auth)[2]["items"] == []
    assert list(conn.iterdump()) == before
    assert request("/api/attention-collect", {}, cookie=cookie)[0] == 403
    assert request("/api/attention-detail", {"action_id":"absent"}, cookie=cookie)[0] == 403
    assert request("/api/attention-detail", {"action_id":"absent"}, **auth)[0] == 409
    assert request("/api/attention-collect", {}, **auth)[2]["created"] == 0


@pytest.mark.parametrize("invalid", [[], {}, 123, True, None])
def test_mail_control_malformed_request_ids_fail_without_mutation(console, conn, invalid):
    request, _ = console
    cookie, csrf = login(request)
    body = {"request_id": invalid, "expected_revision": 0, "enabled": True, "category": "upstream"}
    before = list(conn.iterdump())
    for endpoint in ("attention-subscription-save", "mail-action", "mail-meeting-draft-save", "mail-meeting-prepare", "mail-meeting-cancel"):
        code, _, result = request("/api/" + endpoint, body, cookie=cookie, csrf=csrf)
        assert code == 409 and "error" in result
    assert list(conn.iterdump()) == before


def test_audit_http_is_authenticated_and_readonly(console, conn):
    from test_audit_inventory import populate

    populate(conn, 1)
    request, _ = console
    assert request("/api/audit", {})[0] == 403
    cookie, csrf = login(request)
    assert request("/api/audit", {}, cookie=cookie)[0] == 403
    before = list(conn.iterdump())
    code, _, result = request("/api/audit", {}, cookie=cookie, csrf=csrf)
    assert code == 200 and result["total_matching"] == 1
    assert "PRIVATE" not in str(result)
    assert list(conn.iterdump()) == before


def test_styles_deliver_long_content_and_mobile_rules(console):
    request, _ = console
    status, _, content = request("/styles.css")
    assert status == 200
    css = content.decode()
    assert ".page pre,#features-diff{white-space:pre-wrap;overflow-wrap:anywhere" in css
    assert ":is(.page,#case-dialog) textarea{display:block;width:100%;max-width:100%;min-width:0" in css
    assert "min-height:44px" in css


def test_profile_inventory_http_requires_csrf_and_does_not_write(console, conn):
    request, _ = console
    assert request("/api/requester-profiles", {})[0] == 403
    cookie, csrf = login(request)
    assert request("/api/requester-profiles", {}, cookie=cookie)[0] == 403
    before = list(conn.iterdump())
    code, _, result = request("/api/requester-profiles", {}, cookie=cookie, csrf=csrf)
    assert code == 200 and result["read_only"] and not result["directory_refreshed"]
    assert list(conn.iterdump()) == before


def test_profile_correction_http_trusts_server_actor(console, conn):
    from test_profile_actions import request as correction
    payload = correction(conn)
    request, _ = console
    cookie, csrf = login(request)
    assert request("/api/requester-profile-correct", payload, cookie=cookie)[0] == 403
    code, _, result = request("/api/requester-profile-correct", payload, cookie=cookie, csrf=csrf)
    assert code == 200 and not result["replayed"]
    assert conn.execute("SELECT actor_id FROM profile_actions").fetchone()[0] == "owner-user"


def test_policy_simulation_http_readonly(console, conn):
    from test_policy_simulation import scenario

    request, _ = console
    assert request("/api/policy-simulation", scenario())[0] == 403
    cookie, csrf = login(request)
    before = list(conn.iterdump())
    code, _, result = request("/api/policy-simulation", scenario(), cookie=cookie, csrf=csrf)
    assert code == 200 and result["result"]["route"] == "codex_debug"
    assert list(conn.iterdump()) == before
    payload = {"scenario": scenario(), "proposed_minimum_confidence": 0.99}
    assert request("/api/policy-comparison", payload, cookie=cookie)[0] == 403
    code, _, comparison = request("/api/policy-comparison", payload, cookie=cookie, csrf=csrf)
    assert code == 200 and not comparison["applied"]
    assert comparison["candidate"]["result"]["route"] == "research"
    assert list(conn.iterdump()) == before


def test_knowledge_import_http_preview_then_apply(console, conn, tmp_path):
    from test_knowledge_import_settings import bundle

    request, _ = console
    payload = {"bundle": bundle(tmp_path)}
    assert request("/api/knowledge-import-preview", payload)[0] == 403
    cookie, csrf = login(request)
    auth = {"cookie": cookie, "csrf": csrf}
    assert request("/api/knowledge-import-preview", payload, cookie=cookie)[0] == 403
    code, _, pending = request("/api/knowledge-import-preview", payload, **auth)
    assert code == 200
    assert conn.execute("SELECT count(*) FROM knowledge_entries").fetchone()[0] == 0
    code, _, result = request("/api/knowledge-import-apply", {"draft_id": pending["draft_id"], "actor_id": "forged"}, **auth)
    assert code == 200 and result["actions"]["create"] == 1
    assert conn.execute("SELECT applied_by FROM knowledge_import_drafts").fetchone()[0] == "owner-user"


def test_work_hours_migration_http_is_explicit(console, conn):
    request, _ = console
    cookie, csrf = login(request)
    auth = {"cookie": cookie, "csrf": csrf}
    conn.execute("INSERT INTO work_hours_settings VALUES(1,1,'old-base','{\"start\":\"10:00\",\"end\":\"19:00\"}','owner','2026-09-07T00:00:00+00:00')")
    code, _, state = request("/api/work-hours", {}, **auth)
    assert code == 200 and state["needs_migration"]
    payload = {"expected_revision": 1, "values": state["values"]}
    assert request("/api/work-hours-preview", payload, **auth)[0] == 409
    payload["migrate"] = True
    code, _, pending = request("/api/work-hours-preview", payload, **auth)
    assert code == 200 and pending["migration"]
    assert conn.execute("SELECT base_digest FROM work_hours_settings").fetchone()[0] == "old-base"
    assert request("/api/work-hours-apply", {"draft_id": pending["draft_id"]}, **auth)[0] == 200
    assert not request("/api/work-hours", {}, **auth)[2]["needs_migration"]


def test_execution_stop_http_requires_csrf_and_uses_owner(console, conn, config):
    from test_coding_budget import setup

    _, job = setup(conn, config)
    request, _ = console
    assert request("/api/execution-stop-preview", {"job_id": job})[0] == 403
    cookie, csrf = login(request)
    auth = {"cookie": cookie, "csrf": csrf}
    code, _, shown = request("/api/execution-stop-preview", {"job_id": job}, **auth)
    assert code == 200
    payload = {"job_id": job, "binding_digest": shown["binding_digest"], "request_id": str(uuid.uuid4()), "actor_id": "forged"}
    assert request("/api/execution-stop", payload, cookie=cookie)[0] == 403
    assert request("/api/execution-stop", payload, **auth)[2]["accepted"]
    assert conn.execute("SELECT actor_id FROM execution_stop_requests").fetchone()[0] == "owner-user"


def test_execution_recovery_http_auth_binding_and_identity(console, conn, config):
    from test_broker_recovery import repaired

    job = repaired(conn, config)
    request, _ = console
    assert request("/api/execution-recovery-preview", {"job_id": job})[0] == 403
    cookie, csrf = login(request)
    auth = {"cookie": cookie, "csrf": csrf}
    code, _, shown = request("/api/execution-recovery-preview", {"job_id": job}, **auth)
    assert code == 200
    payload = {"job_id": job, "binding_digest": shown["binding_digest"],
               "request_id": str(uuid.uuid4()), "actor_id": "forged"}
    assert request("/api/execution-recovery", payload, cookie=cookie)[0] == 403
    assert request("/api/execution-recovery", payload, **auth)[2]["accepted"]
    assert conn.execute("SELECT actor_id FROM broker_recovery_actions").fetchone()[0] == "owner-user"
    assert request("/api/execution-recovery", payload, **auth)[2]["replayed"]


def test_execution_inventory_requires_auth_and_is_readonly(console, conn):
    request, _ = console
    assert request("/api/executions", {})[0] == 403
    cookie, csrf = login(request)
    assert request("/api/executions", {}, cookie=cookie)[0] == 403
    before = list(conn.iterdump())
    code, _, result = request("/api/executions", {}, cookie=cookie, csrf=csrf)
    assert code == 200 and result["board"]["physical_state"] == "unknown"
    assert list(conn.iterdump()) == before


def test_retention_inventory_requires_auth_csrf_and_never_mutates(console, conn):
    request, _ = console
    assert request("/api/retention-inventory", {})[0] == 403
    cookie, csrf = login(request)
    assert request("/api/retention-inventory", {}, cookie=cookie)[0] == 403
    before = list(conn.iterdump())
    code, _, result = request("/api/retention-inventory", {}, cookie=cookie, csrf=csrf)
    assert code == 200 and result["read_only"] and result["items"] == []
    assert list(conn.iterdump()) == before


def test_retention_recovery_http_requires_preview_and_csrf(console):
    request, _ = console
    cookie, csrf = login(request)
    payload = {"attempt_id": "fixture"}
    for endpoint in ("retention-recovery-preview", "retention-recovery-apply", "retention-recovery-check"):
        assert request("/api/"+endpoint, payload)[0] == 403
        assert request("/api/"+endpoint, payload, cookie=cookie)[0] == 403
    assert request("/api/retention-recovery-apply", payload, cookie=cookie, csrf=csrf)[0] == 409


@pytest.mark.parametrize("outcome", ["success", "occupied", "post_restore_crash"])
def test_retention_full_http_restore_and_reconcile(console, conn, config, monkeypatch, outcome):
    from test_retention_recheck import candidate

    from k3_support import retention_recovery
    from k3_support.operations import apply_retention

    path, _, planned = candidate(conn, config)
    assert apply_retention(conn, config, planned)["quarantined"] == 1
    attempt = conn.execute("SELECT attempt_id FROM retention_attempts").fetchone()[0]
    request, _ = console
    cookie, csrf = login(request)
    auth = {"cookie": cookie, "csrf": csrf}
    code, _, preview = request("/api/retention-recovery-preview", {"attempt_id": attempt}, **auth)
    assert code == 200 and not path.exists()
    ident = str(uuid.uuid4())
    payload = {"attempt_id": attempt, "binding_digest": preview["binding_digest"], "request_id": ident, "actor_id": "forged"}
    if outcome == "occupied":
        path.write_text("new user file")
    elif outcome == "post_restore_crash":
        original = retention_recovery.recover
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("interrupted after filesystem restore")
        monkeypatch.setattr(retention_recovery, "recover", interrupted)
    code, _, _result = request("/api/retention-recovery-apply", payload, **auth)
    assert code == {"success": 200, "occupied": 409, "post_restore_crash": 503}[outcome]
    assert path.read_text() == ("new user file" if outcome == "occupied" else "retained evidence")
    audit = conn.execute("SELECT actor_id,state FROM retention_recovery_requests WHERE request_id=?", (ident,)).fetchone()
    assert audit["actor_id"] == "owner-user"
    assert audit["state"] == ("restored" if outcome == "success" else "unknown")
    code, _, checked = request("/api/retention-recovery-check", {"request_id": ident}, **auth)
    assert code == 200 and checked["state"] == ("unknown" if outcome == "occupied" else "restored")
    code, _, replay = request("/api/retention-recovery-apply", payload, **auth)
    assert code == 200 and replay["replayed"]
    assert conn.execute("SELECT count(*) FROM retention_recovery_requests").fetchone()[0] == 1


@pytest.mark.parametrize("endpoint,module,function", [
    ("remote-observe", "broker_remote_observation", "record"),
    ("remote-cleanup-preview", "broker_remote_cleanup", "preview"),
    ("remote-cleanup-apply", "broker_remote_cleanup", "apply"),
])
def test_remote_cleanup_http_requires_login_and_csrf(console, monkeypatch, endpoint, module, function):
    request, _ = console
    calls = []
    def action(conn, cfg, **values):
        calls.append(values)
        return {"state": "unknown"}
    monkeypatch.setattr(f"k3_support.{module}.{function}", action)
    payload = {"request_id": str(uuid.uuid4()), "observation_id": str(uuid.uuid4()), "preview_digest": "fixture", "actor_uid": 0}
    assert request("/api/"+endpoint, payload)[0] == 403
    cookie, csrf = login(request)
    assert request("/api/"+endpoint, payload, cookie=cookie)[0] == 403
    assert calls == []
    assert request("/api/"+endpoint, payload, cookie=cookie, csrf=csrf)[0] == 200
    assert len(calls) == 1 and "actor_uid" not in calls[0]


def test_knowledge_lifecycle_http_uses_server_identity(console, conn):
    from test_knowledge_lifecycle import request as action_request
    from test_knowledge_preview import candidate

    identifier = candidate(conn)
    payload = action_request(conn, identifier)
    payload["actor_id"] = "forged"
    request, _ = console
    assert request("/api/knowledge-lifecycle", payload)[0] == 403
    cookie, csrf = login(request)
    assert request("/api/knowledge-lifecycle", payload, cookie=cookie)[0] == 403
    auth = {"cookie": cookie, "csrf": csrf}
    assert request("/api/knowledge-lifecycle", payload, **auth)[0] == 200
    assert conn.execute("SELECT actor_id FROM knowledge_lifecycle_actions").fetchone()[0] == "owner-user"
    assert request("/api/knowledge-lifecycle", {**payload, "decision": "approved"}, **auth)[0] == 409


def test_knowledge_list_requires_session_and_csrf(console, conn):
    from test_knowledge_inventory import entry

    identifier = entry(conn, 1)
    request, _ = console
    payload = {"query": "风扇", "status": "candidate"}
    assert request("/api/knowledge-list", payload)[0] == 403
    cookie, csrf = login(request)
    assert request("/api/knowledge-list", payload, cookie=cookie)[0] == 403
    auth = {"cookie": cookie, "csrf": csrf}
    before = list(conn.iterdump())
    code, _, result = request("/api/knowledge-list", payload, **auth)
    assert code == 200 and result["items"][0]["knowledge_id"] == identifier
    assert list(conn.iterdump()) == before
    assert request("/api/knowledge-list", {"status": []}, **auth)[0] == 409


def test_knowledge_detail_http_is_version_bound_and_readonly(console,conn):
    from test_knowledge_preview import candidate

    identifier=candidate(conn,answer="<script>untrusted</script>"+"完整内容"*2000)
    request,_=console
    payload={"knowledge_id":identifier,"page":1}
    assert request("/api/knowledge-detail",payload)[0] == 403
    cookie,csrf=login(request)
    auth={"cookie":cookie,"csrf":csrf}
    first=request("/api/knowledge-detail",payload,**auth)[2]
    assert first["read_only"] and first["page_count"]>1
    assert "buttons" not in first and "callback_data" not in str(first)
    assert request("/api/knowledge-detail",{**payload,"page":2},**auth)[0] == 409
    next_page={**payload,"page":2,"content_digest":first["content_digest"]}
    assert request("/api/knowledge-detail",next_page,**auth)[0] == 200
    conn.execute("UPDATE knowledge_entries SET answer_markdown='updated' WHERE knowledge_id=?",(identifier,))
    assert request("/api/knowledge-detail",next_page,**auth)[0] == 409
    assert conn.execute("SELECT status FROM knowledge_entries WHERE knowledge_id=?",(identifier,)).fetchone()[0] == "candidate"


def test_budget_view_requires_login_and_does_not_claim_global_coverage(console,conn):
    request,_=console
    assert request("/api/model-budget")[0] == 403
    cookie,_=login(request)
    result=request("/api/model-budget",cookie=cookie)
    assert result[0] == 200 and not result[2]["configured"]
    assert not result[2]["coverage"]["global_budget_enforced"]
    assert "coding_internal_model_turns" in result[2]["coverage"]["not_guarded"]
    assert conn.execute("SELECT count(*) FROM model_budget_policy").fetchone()[0] == 0


def test_budget_editor_http_confirmed_session_bound_policy(console,conn):
    from test_budget_settings import VALUES

    request,_=console
    body={"values":VALUES,"expected_revision":0}
    assert request("/api/budget-preview",body)[0] == 403
    cookie,csrf=login(request)
    auth={"cookie":cookie,"csrf":csrf}
    assert request("/api/budget-preview",body,cookie=cookie)[0] == 403
    draft=request("/api/budget-preview",body,**auth)[2]
    assert conn.execute("SELECT count(*) FROM model_budget_policy").fetchone()[0] == 0
    result=request("/api/budget-apply",{"draft_id":draft["draft_id"]},**auth)
    assert result[0] == 200 and result[2]["revision"] == 1
    assert request("/api/budget-apply",{"draft_id":draft["draft_id"]},**auth)[2]["replayed"]


def test_mail_meeting_draft_gui_is_local_and_authenticated(console, conn):
    from test_mail_snapshot import item

    message, _, _ = item(conn, 2)
    request, _ = console
    assert request("/api/mail-meeting-draft", {"message_id":message})[0] == 403
    cookie, csrf = login(request)
    auth = {"cookie":cookie,"csrf":csrf}
    value = request("/api/mail-meeting-draft", {"message_id":message}, **auth)[2]
    body = {"message_id":message,"draft":value["draft"],"expected_revision":0,
            "source_digest":value["source_digest"],"request_id":str(uuid.uuid4())}
    assert request("/api/mail-meeting-draft-save", body, cookie=cookie)[0] == 403
    result = request("/api/mail-meeting-draft-save", body, **auth)
    assert result[0] == 200 and not result[2]["calendar_created"]
    assert request("/api/mail-meeting-draft-save", body, **auth)[2]["replayed"]
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    prepare_body = {"message_id":message,"expected_revision":1,
                    "source_digest":value["source_digest"],"request_id":str(uuid.uuid4())}
    assert request("/api/mail-meeting-prepare",prepare_body,cookie=cookie)[0] == 403
    # Empty draft has no attendee/time or linked Case; it cannot enter preparation.
    assert request("/api/mail-meeting-prepare",prepare_body,**auth)[0] == 409


def test_mail_preparation_cancel_http_auth_and_replay(console,conn):
    from test_mail_meeting_prepare import setup

    from k3_support import mail_meeting_drafts, mail_meeting_prepare

    message,args=setup(conn)
    queued=mail_meeting_prepare.enqueue(conn,**args)
    binding=mail_meeting_drafts.read(conn,message)["preparation"]["binding_digest"]
    request,_=console
    cookie,csrf=login(request)
    body={"prepare_request_id":queued["request_id"],"binding_digest":binding,"request_id":str(uuid.uuid4())}
    assert request("/api/mail-meeting-cancel",body,cookie=cookie)[0] == 403
    assert request("/api/mail-meeting-cancel",body,cookie=cookie,csrf=csrf)[2]["cancelled"]
    assert request("/api/mail-meeting-cancel",body,cookie=cookie,csrf=csrf)[2]["replayed"]
    assert mail_meeting_drafts.read(conn,message)["preparation"]["state"] == "cancelled"


def test_mail_gui_auth_replay_and_stale_state(console, conn):
    from test_mail_snapshot import item

    message, _, _ = item(conn, 1)
    request, _ = console
    assert request("/api/mail-list", {})[0] == 403
    cookie, csrf = login(request)
    auth = {"cookie": cookie, "csrf": csrf}
    value = request("/api/mail-list", {}, **auth)[2]["items"][0]
    body = {"message_id": message, "action": "done", "expected_revision": value["revision"],
            "content_digest": value["content_digest"], "request_id": str(uuid.uuid4()),
            "actor_id": "forged"}
    assert request("/api/mail-action", body, cookie=cookie)[0] == 403
    assert request("/api/mail-action", body, **auth)[0] == 200
    assert request("/api/mail-action", body, **auth)[2]["replayed"]
    assert request("/api/mail-action", {**body,"request_id":str(uuid.uuid4())}, **auth)[0] == 409
    row = conn.execute("SELECT * FROM mail_action_state").fetchone()
    assert row["updated_by"] != "forged" and row["state"] == "done"


def test_notification_snooze_http_auth_revision_and_resume(console, conn):
    request, _ = console
    body={"minutes":60,"expected_revision":0,"request_id":str(uuid.uuid4())}
    assert request("/api/notification-snooze",body)[0] == 403
    cookie,csrf=login(request)
    assert request("/api/notification-snooze",body,cookie=cookie)[0] == 403
    assert request("/api/notification-snooze",body,cookie=cookie,csrf=csrf)[0] == 200
    state=request("/api/status",cookie=cookie)[2]["notification_snooze"]
    assert state["active"] and state["revision"] == 1
    assert request("/api/notification-snooze",body,cookie=cookie,csrf=csrf)[2]["replayed"]
    assert request("/api/notification-snooze",{**body,"request_id":str(uuid.uuid4())},cookie=cookie,csrf=csrf)[0] == 409
    resume={"minutes":0,"expected_revision":1,"request_id":str(uuid.uuid4())}
    assert request("/api/notification-snooze",resume,cookie=cookie,csrf=csrf)[0] == 200
    assert not request("/api/status",cookie=cookie)[2]["notification_snooze"]["active"]
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


def test_night_toggle_http_is_separate_from_manual_resume(console):
    request,_=console
    cookie,csrf=login(request)
    body={"night_enabled":True,"expected_revision":0,"request_id":str(uuid.uuid4())}
    assert request("/api/notification-snooze",body,cookie=cookie,csrf=csrf)[0] == 200
    state=request("/api/status",cookie=cookie)[2]["notification_snooze"]
    assert state["night_enabled"] and not state["manual_active"]
    resume={"minutes":0,"expected_revision":1,"request_id":str(uuid.uuid4())}
    assert request("/api/notification-snooze",resume,cookie=cookie,csrf=csrf)[0] == 200
    assert request("/api/status",cookie=cookie)[2]["notification_snooze"]["night_enabled"]


def test_feature_editor_http_auth_preview_apply_and_rollback(console, config, conn):
    request, _ = console
    assert request("/api/features")[0] == 403
    cookie, csrf = login(request)
    code, _, initial = request("/api/features", cookie=cookie)
    assert code == 200 and initial["revision"] == 0
    body = {"expected_revision":0, "values":{**initial["values"], "codex":True}}
    assert request("/api/features-preview", body, cookie=cookie)[0] == 403
    code, _, draft = request("/api/features-preview", body, cookie=cookie, csrf=csrf)
    assert code == 200 and not config.feature("codex")
    another, another_csrf = login(request)
    assert request("/api/features-apply", {"draft_id":draft["draft_id"]}, cookie=another, csrf=another_csrf)[0] == 409
    code, _, applied = request("/api/features-apply", {"draft_id":draft["draft_id"]}, cookie=cookie, csrf=csrf)
    assert code == 200 and applied["revision"] == 1 and config.feature("codex")
    assert request("/api/features-apply", {"draft_id":draft["draft_id"]}, cookie=cookie, csrf=csrf)[2]["replayed"]
    code, _, rollback = request("/api/features-preview", {"expected_revision":1,"rollback_revision":1}, cookie=cookie, csrf=csrf)
    assert code == 200 and config.feature("codex")
    assert request("/api/features-apply", {"draft_id":rollback["draft_id"]}, cookie=cookie, csrf=csrf)[0] == 200
    assert not config.feature("codex")
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_feature_editor_requires_explicit_base_migration(console, config):
    request, _ = console
    cookie, csrf = login(request)
    values = {**config.raw["features"], "codex":True}
    code, _, initial = request("/api/features-preview", {"expected_revision":0,"values":values}, cookie=cookie, csrf=csrf)
    assert code == 200
    assert request("/api/features-apply", {"draft_id":initial["draft_id"]}, cookie=cookie, csrf=csrf)[0] == 200
    config.raw["work_hours"]["start"] = "10:00"
    code, _, editor = request("/api/features", cookie=cookie)
    assert code == 200 and editor["requires_rebase"]
    assert not any(editor["values"].values())
    body = {"expected_revision":1,"values":editor["values"]}
    assert request("/api/features-preview", body, cookie=cookie, csrf=csrf)[0] == 409
    code, _, proposal = request("/api/features-preview", {**body,"rebase":True}, cookie=cookie, csrf=csrf)
    assert code == 200 and proposal["rebase"]
    assert request("/api/features-apply", {"draft_id":proposal["draft_id"]}, cookie=cookie, csrf=csrf)[0] == 200
    current = request("/api/features", cookie=cookie)[2]
    assert current["revision"] == 2 and not current["requires_rebase"]
    assert not config.feature("codex")


def test_case_detail_is_authenticated_read_only_and_version_bound(console, conn):
    from test_coordination import make_turn

    case_id, _, _ = make_turn(conn)
    request, _ = console
    body = {"case_id": case_id}
    assert request("/api/case-detail", body)[0] == 403
    cookie, csrf = login(request)
    assert request("/api/case-detail", body, cookie=cookie)[0] == 403
    before = dict(
        conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    )
    code, _, detail = request("/api/case-detail", body, cookie=cookie, csrf=csrf)
    assert code == 200
    assert detail["read_only"] and "K3 启动失败" in detail["text"]
    assert "buttons" not in detail and "callback_data" not in detail
    assert before == dict(
        conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    )
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    for invalid in ({"page": True}, {"page": 2}, {"case_id": "../../config"}):
        assert (
            request("/api/case-detail", {**body, **invalid}, cookie=cookie, csrf=csrf)[
                0
            ]
            == 409
        )
    conn.execute("UPDATE cases SET title='新的调查进展' WHERE case_id=?", (case_id,))
    assert (
        request(
            "/api/case-detail",
            {**body, "content_digest": detail["content_digest"]},
            cookie=cookie,
            csrf=csrf,
        )[0]
        == 409
    )
    assert request("/api/case-detail", body, cookie=cookie, csrf=csrf)[0] == 200


def test_http_auth_origin_csrf_host_and_logout(console):
    request, _ = console
    assert request("/api/status")[0] == 403
    assert request("/api/login", {"key": "a" * 64}, origin=False)[0] == 403
    assert request("/", host="attacker.example")[0] == 403
    cookie, csrf = login(request)
    assert request("/api/status", cookie=cookie)[0] == 200
    assert request("/api/panel", {}, cookie=cookie, csrf="wrong")[0] == 403
    assert request("/api/logout", {}, cookie=cookie, csrf=csrf)[0] == 200
    assert request("/api/status", cookie=cookie)[0] == 403


@pytest.mark.parametrize("web_only", [False, True])
def test_case_actions_session_binding_freshness_and_idempotency(console, conn, config, web_only):
    if web_only:
        config.raw["identity"].update(control_operator_id="web-owner", telegram_control_user_id=None, telegram_control_chat_id=None)
    from test_coordination import make_turn

    from k3_support.coordination import control_communication

    case_id, _, turn = make_turn(conn)
    request, _ = console
    cookie, csrf = login(request)

    def detail():
        code, _, value = request(
            "/api/case-detail", {"case_id": case_id}, cookie=cookie, csrf=csrf
        )
        assert code == 200
        return value

    buttons = detail()["actions"]
    claim = next(b for b in buttons if b["label"] == "我来回复")
    body = {"token": claim["token"], "request_id": str(uuid.uuid4())}
    other, other_csrf = login(request)
    assert request("/api/case-action", body, cookie=other, csrf=other_csrf)[0] == 409
    assert request("/api/case-action", body, cookie=cookie, csrf="wrong")[0] == 403
    code, _, result = request("/api/case-action", body, cookie=cookie, csrf=csrf)
    assert code == 200 and result["action"] == "claim"
    after = dict(
        conn.execute(
            "SELECT * FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)
        ).fetchone()
    )
    assert after["communication_owner"] == "human"
    assert request("/api/case-action", body, cookie=cookie, csrf=csrf)[2] == result
    assert after == dict(
        conn.execute(
            "SELECT * FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)
        ).fetchone()
    )
    assert (
        conn.execute(
            "SELECT external_id FROM operator_activities ORDER BY rowid DESC LIMIT 1"
        )
        .fetchone()[0]
        .startswith("gui:")
    )
    stale = next(b for b in detail()["actions"] if b["label"] == "交给 AI")
    control_communication(
        conn,
        case_id=case_id,
        action="suggest_only",
        actor_id="owner",
        external_id="telegram:test-race",
    )
    assert (
        request(
            "/api/case-action",
            {"token": stale["token"], "request_id": str(uuid.uuid4())},
            cookie=cookie,
            csrf=csrf,
        )[0]
        == 409
    )
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("web_only", [False, True])
def test_mode_changes_require_current_session_panel_and_confirmation(console, conn, config, web_only):
    if web_only:
        config.raw["identity"].update(control_operator_id="web-owner", telegram_control_user_id=None, telegram_control_chat_id=None)
    request, _ = console
    cookie, csrf = login(request)
    code, _, panel = request("/api/panel", {}, cookie=cookie, csrf=csrf)
    assert code == 200
    action = lambda callback: request(
        "/api/action", {"callback": callback}, cookie=cookie, csrf=csrf
    )
    # A caller cannot skip the exact two-step confirmation.
    assert action(f"fsc:A:{panel['panel_id']}")[0] == 409
    code, _, confirmation = action(f"fsc:a:{panel['panel_id']}")
    assert code == 200
    assert any(
        button["callback_data"].startswith("fsc:A:")
        for button in confirmation["buttons"]
    )
    code, _, changed = action(f"fsc:A:{panel['panel_id']}")
    assert code == 200 and changed["mode"] == "auto"
    assert action(f"fsc:A:{panel['panel_id']}")[0] == 409
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    # Enabling runtime auto does not change the static Shadow safety gate.
    assert request("/api/status", cookie=cookie)[2]["static_mode"] == "shadow"
    assert action(f"fsc:p:{panel['panel_id']}")[2]["mode"] == "paused"


def test_case_action_post_commit_error_cannot_repeat_execution(
    console, conn, monkeypatch
):
    from test_coordination import make_turn

    from k3_support import gui

    case_id, _, _ = make_turn(conn)
    request, _ = console
    cookie, csrf = login(request)
    detail = request(
        "/api/case-detail", {"case_id": case_id}, cookie=cookie, csrf=csrf
    )[2]
    token = next(b["token"] for b in detail["actions"] if b["label"] == "我来回复")
    original, calls = gui.execute_control, []

    def interrupted(*args, **kwargs):
        calls.append(1)
        original(*args, **kwargs)
        raise ValueError("synthetic post-commit failure")

    monkeypatch.setattr(gui, "execute_control", interrupted)
    payload = {"token": token, "request_id": str(uuid.uuid4())}
    assert request("/api/case-action", payload, cookie=cookie, csrf=csrf)[0] == 409
    assert request("/api/case-action", payload, cookie=cookie, csrf=csrf)[0] == 409
    assert len(calls) == 1
    assert (
        conn.execute(
            "SELECT communication_owner FROM conversation_turns WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]
        == "human"
    )


def test_gui_does_not_invalidate_telegram_just_by_opening(console, conn, config):
    telegram = issue_global_panel(
        conn,
        config,
        operator_user_id="owner-user",
        chat_id="owner-chat",
        command_message_id="tg-command",
    )
    bind_global_panel(
        conn,
        panel_id=telegram["panel_id"],
        operator_user_id="owner-user",
        chat_id="owner-chat",
        command_message_id="tg-command",
        prompt_message_id="tg-prompt",
    )
    request, _ = console
    cookie, csrf = login(request)
    panel = request("/api/panel", {}, cookie=cookie, csrf=csrf)[2]
    assert (
        conn.execute(
            "SELECT state FROM global_control_panels WHERE panel_id=?",
            (telegram["panel_id"],),
        ).fetchone()[0]
        == "active"
    )
    cookie2, csrf2 = login(request)
    assert (
        request(
            "/api/action",
            {"callback": f"fsc:p:{panel['panel_id']}"},
            cookie=cookie2,
            csrf=csrf2,
        )[0]
        == 409
    )
    assert (
        request(
            "/api/action",
            {"callback": f"fsc:p:{panel['panel_id']}"},
            cookie=cookie,
            csrf=csrf,
        )[0]
        == 200
    )
    assert (
        conn.execute(
            "SELECT state FROM global_control_panels WHERE panel_id=?",
            (telegram["panel_id"],),
        ).fetchone()[0]
        == "retired"
    )


def test_assets_have_no_inline_script_or_dom_html_injection(console):
    request, _ = console
    status, headers, page = request("/")
    assert status == 200 and b'lang="zh-CN"' in page
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert request("/../../config.yaml")[0] == 403
    script = request("/app.js")[2]
    assert b"innerHTML" not in script and b"textContent" in script
    assert request("/styles.css")[0] == 200


def test_login_throttling_and_expiration(console):
    request, app = console
    for _ in range(10):
        assert request("/api/login", {"key": "wrong"})[0] == 409
    assert request("/api/login", {"key": "a" * 64})[0] == 409
    app.failures.clear()
    cookie, _ = login(request)
    app.sessions[cookie.split("=", 1)[1]]["expires"] = 0
    assert request("/api/status", cookie=cookie)[0] == 403


def test_action_request_replay_does_not_renew_or_change_mode(console, conn):
    request, _ = console
    cookie, csrf = login(request)
    panel = request("/api/panel", {}, cookie=cookie, csrf=csrf)[2]
    command = {
        "callback": f"fsc:t:{panel['panel_id']}",
        "request_id": str(uuid.uuid4()),
    }
    first = request("/api/action", command, cookie=cookie, csrf=csrf)
    assert first[0] == 200
    state = tuple(
        conn.execute(
            "SELECT revision,auto_expires_at FROM global_control_state"
        ).fetchone()
    )
    assert request("/api/action", command, cookie=cookie, csrf=csrf)[2] == first[2]
    assert (
        tuple(
            conn.execute(
                "SELECT revision,auto_expires_at FROM global_control_state"
            ).fetchone()
        )
        == state
    )
    command["callback"] = f"fsc:p:{panel['panel_id']}"
    assert request("/api/action", command, cookie=cookie, csrf=csrf)[0] == 409


def test_gui_resolve_and_reopen_require_separate_confirmation(console, conn):
    from test_coordination import make_turn

    case_id, _, _ = make_turn(conn)
    request, _ = console
    cookie, csrf = login(request)

    def detail():
        return request(
            "/api/case-detail", {"case_id": case_id}, cookie=cookie, csrf=csrf
        )[2]

    def click(token):
        return request(
            "/api/case-action",
            {"token": token, "request_id": str(uuid.uuid4())},
            cookie=cookie,
            csrf=csrf,
        )

    initial = dict(
        conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    )
    token = next(b["token"] for b in detail()["actions"] if b["label"] == "标记解决")
    code, _, pending = click(token)
    assert code == 200 and pending["requires_confirmation"]
    assert initial == dict(
        conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    )
    # The first-stage token is consumed; only the newly issued confirmation acts.
    assert click(token)[0] == 409
    code, _, result = click(pending["confirmation"]["token"])
    assert code == 200 and result["state"] == "resolved"
    closed = request("/api/workbench", {"view": "closed"}, cookie=cookie, csrf=csrf)
    assert closed[0] == 200 and any(
        item["case_id"] == case_id for item in closed[2]["items"]
    )
    token = next(
        b["token"] for b in detail()["actions"] if b["label"] == "重新打开（人工负责）"
    )
    pending = click(token)[2]
    assert (
        conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()[
            0
        ]
        == "resolved"
    )
    assert click(pending["confirmation"]["token"])[0] == 200
    after = dict(
        conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    )
    assert after["state"] == "triage" and after["owner"] == "operator"
    assert after["lifecycle_round"] == initial["lifecycle_round"] + 1
    assert (
        conn.execute(
            "SELECT count(*) FROM jobs WHERE state IN ('queued','running')"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT external_id FROM case_lifecycle_actions LIMIT 1")
        .fetchone()[0]
        .startswith("gui:")
    )


def test_gui_confirmation_is_revoked_by_detail_refresh(console, conn):
    from test_coordination import make_turn

    case_id, _, _ = make_turn(conn)
    request, _ = console
    cookie, csrf = login(request)
    body = {"case_id": case_id}
    detail = request("/api/case-detail", body, cookie=cookie, csrf=csrf)[2]
    token = next(b["token"] for b in detail["actions"] if b["label"] == "标记解决")
    pending = request(
        "/api/case-action",
        {"token": token, "request_id": str(uuid.uuid4())},
        cookie=cookie,
        csrf=csrf,
    )[2]
    request("/api/case-detail", body, cookie=cookie, csrf=csrf)
    assert (
        request(
            "/api/case-action",
            {
                "token": pending["confirmation"]["token"],
                "request_id": str(uuid.uuid4()),
            },
            cookie=cookie,
            csrf=csrf,
        )[0]
        == 409
    )


def test_gui_queue_pages_keep_opening_range_and_reject_bad_cursor(console, conn):
    from test_coordination import make_turn

    for index in range(31):
        make_turn(
            conn, message_id=f"om_gui_page_{index}", chat_id=f"oc_gui_page_{index}"
        )
    request, _ = console
    cookie, csrf = login(request)

    def page(body):
        return request("/api/workbench", body, cookie=cookie, csrf=csrf)

    first = page({})[2]
    assert len(first["items"]) == 30 and first["next_cursor"]
    late, _, _ = make_turn(conn, message_id="om_late", chat_id="oc_late")
    second = page({"cursor": first["next_cursor"]})[2]
    assert len(second["items"]) == 1
    assert second["items"][0]["case_id"] != late
    assert not {i["case_id"] for i in first["items"]} & {
        i["case_id"] for i in second["items"]
    }
    assert page({})[2]["total_items"] == 32
    assert page({"cursor": "invalid"})[0] == 409
    assert page({"view": "invalid"})[0] == 409


@pytest.mark.parametrize("approve", [True, False])
@pytest.mark.parametrize("web_only", [False, True])
def test_gui_board_approval_exact_content_channel_and_repeat(console, conn, config, approve, web_only):
    if web_only:
        config.raw["identity"].update(control_operator_id="web-owner", telegram_control_user_id=None, telegram_control_chat_id=None)
    from test_coordination import make_turn

    from k3_support.approvals import (
        expiry_after,
        normalized_board_action,
        request_approval,
    )

    case_id, _, _ = make_turn(conn)
    approval_id, _, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="gui-test",
        action=normalized_board_action(case_id, "gui-test", 15),
        expires_at=expiry_after(15),
    )
    request, _ = console
    cookie, csrf = login(request)
    detail = request(
        "/api/approval-detail", {"approval_id": approval_id}, cookie=cookie, csrf=csrf
    )
    assert detail[0] == 200 and '"estimated_minutes": 15' in detail[2]["text"]
    token = next(
        b["token"]
        for b in detail[2]["actions"]
        if b["label"] == ("同意" if approve else "不同意")
    )
    command = {"token": token, "request_id": str(uuid.uuid4())}
    other, other_csrf = login(request)
    assert (
        request("/api/approval-action", command, cookie=other, csrf=other_csrf)[0]
        == 409
    )
    code, _, response = request(
        "/api/approval-action", command, cookie=cookie, csrf=csrf
    )
    assert code == 200 and response["status"] == ("approved" if approve else "denied")
    row = dict(
        conn.execute(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
    )
    assert row["approver_channel"] == "gui"
    if web_only:
        assert row["approver_identity"] == "web-owner"
    assert (
        request("/api/approval-action", command, cookie=cookie, csrf=csrf)[2]
        == response
    )
    assert row == dict(
        conn.execute(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
    )
    assert conn.execute(
        "SELECT count(*) FROM locks WHERE lock_key='board1'"
    ).fetchone()[0] == int(approve)


def test_gui_approval_rejects_case_changes_before_commit(console, conn):
    from test_coordination import make_turn

    from k3_support.approvals import (
        expiry_after,
        normalized_board_action,
        request_approval,
    )

    case_id, _, _ = make_turn(conn)
    approval_id, _, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="gui-stale",
        action=normalized_board_action(case_id, "gui-stale", 15),
        expires_at=expiry_after(15),
    )
    request, _ = console
    cookie, csrf = login(request)
    detail = request(
        "/api/approval-detail", {"approval_id": approval_id}, cookie=cookie, csrf=csrf
    )[2]
    token = next(b["token"] for b in detail["actions"] if b["label"] == "同意")
    conn.execute("UPDATE cases SET version=version+1 WHERE case_id=?", (case_id,))
    assert (
        request(
            "/api/approval-action",
            {"token": token, "request_id": str(uuid.uuid4())},
            cookie=cookie,
            csrf=csrf,
        )[0]
        == 409
    )
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()[0]
        == "requested"
    )
    assert conn.execute("SELECT count(*) FROM locks").fetchone()[0] == 0


def test_gui_push_exact_action_and_meeting_remains_preview_only(console, conn):
    from test_coordination import make_turn

    from k3_support.approvals import (
        expiry_after,
        normalized_push_action,
        request_approval,
    )

    case_id, _, _ = make_turn(conn)
    action = normalized_push_action(
        case_id=case_id,
        repo="test-repo",
        destination="refs/for/main%wip",
        commits=["a" * 40],
        command=["git", "push", "origin", "HEAD:refs/for/main%wip"],
    )
    approval_id, _, _ = request_approval(
        conn,
        approval_type="wip_push",
        case_id=case_id,
        action=action,
        expires_at=expiry_after(15),
    )
    request, _ = console
    cookie, csrf = login(request)
    detail = request(
        "/api/approval-detail", {"approval_id": approval_id}, cookie=cookie, csrf=csrf
    )[2]
    assert "a" * 40 in detail["text"] and "HEAD:refs/for/main%wip" in detail["text"]
    token = next(b["token"] for b in detail["actions"] if b["label"] == "同意")
    assert (
        request(
            "/api/approval-action",
            {"token": token, "request_id": str(uuid.uuid4())},
            cookie=cookie,
            csrf=csrf,
        )[2]["status"]
        == "approved"
    )
    assert (
        conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    )  # Shadow does not execute push.
    meeting_id, _, _ = request_approval(
        conn,
        approval_type="meeting_create",
        case_id=case_id,
        action={"subject": "synthetic meeting"},
        expires_at=expiry_after(15),
    )
    meeting = request(
        "/api/approval-detail", {"approval_id": meeting_id}, cookie=cookie, csrf=csrf
    )[2]
    assert meeting["actions"] == [] and "完整审批预览" in meeting["note"]


@pytest.mark.parametrize("timeout", [False, True, "partial"])
def test_gui_meeting_approval_only_queues_and_worker_never_retries(
    console, conn, config, timeout
):
    from test_meeting_recovery import prepared

    from k3_support.meeting_dispatch import dispatch_one

    cfg, transport, _, preview = prepared(conn, config)
    # Synthetic setup returns an approved preview. Revert only fixture state to
    # exercise the real HTTP approval instead of fabricating a GUI receipt.
    conn.execute(
        "UPDATE approvals SET status='requested',decided_at=NULL,approver_channel=NULL,approver_identity=NULL,decision_text=NULL WHERE approval_id=?",
        (preview["approval_id"],),
    )
    request, app = console
    app.config = cfg
    cookie, csrf = login(request)
    transport.calls.clear()
    detail = request(
        "/api/approval-detail",
        {"approval_id": preview["approval_id"]},
        cookie=cookie,
        csrf=csrf,
    )
    assert detail[0] == 200
    assert "完整创建请求" in detail[2]["text"] and "完整邀请请求" in detail[2]["text"]
    button = next(
        b
        for b in detail[2]["actions"]
        if "创建" in b["label"] and b["label"] != "不创建"
    )
    command = {"token": button["token"], "request_id": str(uuid.uuid4())}
    assert request("/api/approval-action", command, cookie=cookie, csrf=csrf)[0] == 200
    assert not transport.calls
    assert (
        conn.execute("SELECT state FROM meeting_dispatch_queue").fetchone()[0]
        == "queued"
    )
    assert request("/api/approval-action", command, cookie=cookie, csrf=csrf)[0] == 200
    assert (
        conn.execute("SELECT count(*) FROM meeting_dispatch_queue").fetchone()[0] == 1
    )

    def runner(argv):
        if timeout is True and argv[:3] == ["calendar", "events", "create"]:
            raise TimeoutError("synthetic lost receipt")
        return transport(argv)

    assert dispatch_one(conn, config, runner=runner) is None  # Shadow cannot dispatch.
    from k3_support.runtime_control import current_global_state, ensure_global_state

    mode = current_global_state(conn, cfg)["mode"]
    ensure_global_state(conn)
    conn.execute("UPDATE global_control_state SET mode='paused'")
    assert dispatch_one(conn, cfg, runner=runner) is None
    assert (
        conn.execute("SELECT state FROM meeting_dispatch_queue").fetchone()[0]
        == "queued"
    )
    conn.execute("UPDATE global_control_state SET mode=?", (mode,))
    if timeout == "partial":
        transport.attendee_pages = [{"items": [], "has_more": False}]
    result = dispatch_one(conn, cfg, runner=runner)
    assert result["state"] == ("needs_review" if timeout else "finished")
    calls = list(transport.calls)
    assert dispatch_one(conn, cfg, runner=runner) is None
    assert transport.calls == calls
    latest = request(
        "/api/approval-detail",
        {"approval_id": preview["approval_id"]},
        cookie=cookie,
        csrf=csrf,
    )
    assert latest[0] == 200 and latest[2]["actions"] == []
    assert "后台派发记录" in latest[2]["text"]


def test_coding_executor_catalog_requires_auth_and_rejects_client_paths(console, config, tmp_path):
    from test_coding_catalog import configure
    configure(config, tmp_path, 'claude')
    request, _ = console
    assert request('/api/coding-executors', {})[0] == 403
    cookie, csrf = login(request)
    assert request('/api/coding-executors', {}, cookie=cookie)[0] == 403
    auth = {'cookie': cookie, 'csrf': csrf}
    assert request('/api/coding-executors', {'contract_directory': '/private'}, **auth)[0] == 409
    code, _, result = request('/api/coding-executors', {}, **auth)
    assert code == 200 and result['items'][0]['agent'] == 'claude'
    assert result['worker_health'] == 'not_checked'
    assert str(tmp_path) not in str(result)


def test_coding_task_http_authorization_freshness_and_receipt(console, conn, config, tmp_path):
    from test_coding_tasks import request_fixture
    cfg, payload = request_fixture(conn, config, tmp_path, 'hermes')
    config.raw.update(cfg.raw)
    request, _ = console
    for route, body in [('/api/coding-task', payload), ('/api/coding-task-options', {'case_id':payload['case_id']})]:
        assert request(route, body)[0] == 403
    cookie, csrf = login(request)
    assert request('/api/coding-task', payload, cookie=cookie)[0] == 403
    auth = dict(cookie=cookie, csrf=csrf)
    code, _, options = request('/api/coding-task-options', {'case_id':payload['case_id']}, **auth)
    assert code == 200 and options['case_version'] == 2
    assert options['repositories'] == ['u-boot'] and options['items'][0]['agent'] == 'hermes'
    assert str(tmp_path) not in str(options)
    assert request('/api/coding-task', payload | {'case_version':1}, **auth)[0] == 409
    assert request('/api/coding-task', payload | {'contract_directory':'/private'}, **auth)[0] == 409
    code, _, result = request('/api/coding-task', payload, **auth)
    assert code == 200 and result['created'] and result['state'] == 'queued'
    code, _, replay = request('/api/coding-task', payload, **auth)
    assert code == 200 and not replay['created'] and replay['job_id'] == result['job_id']
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1


@pytest.mark.parametrize("endpoint", ["release-impact-list", "mail-digest-list"])
def test_private_notification_inventory_requires_login_and_csrf(console, endpoint):
    request, _ = console
    path = "/api/" + endpoint
    assert request(path, {})[0] == 403
    cookie, csrf = login(request)
    assert request(path, {}, cookie=cookie)[0] == 403
    auth = dict(cookie=cookie, csrf=csrf)
    assert request(path, {"destination": "untrusted"}, **auth)[0] == 409
    code, _, value = request(path, {}, **auth)
    assert code == 200 and value["read_only"] and value["items"] == []


@pytest.mark.parametrize("endpoint,key", [("release-impact-detail", "impact_id"),
                                          ("mail-digest-detail", "digest_id")])
def test_private_notification_detail_rejects_untrusted_access(console, endpoint, key):
    request, _ = console
    path = "/api/" + endpoint
    payload = {key: "nonexistent"}
    assert request(path, payload)[0] == 403
    cookie, csrf = login(request)
    assert request(path, payload, cookie=cookie)[0] == 403
    assert request(path, payload, cookie=cookie, csrf=csrf)[0] == 409
