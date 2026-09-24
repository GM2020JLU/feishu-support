"""Chat grants use native identity, exact local Bug scope and immutable messages."""

import json

import pytest
from test_project_bugs import binding, observe
from test_project_chat_control import message

from k3_support.approvals import ApprovalError
from k3_support.control import ControlMessage, execute_control
from k3_support.project_bugs import BugConflict


def issue_command(bug_id, *, comment=True, fields=None):
    value = {"comment": comment, "fields": ["title"] if fields is None else fields}
    return f"bug grant {bug_id} 8 '{json.dumps(value, separators=(',', ':'))}'"


@pytest.mark.parametrize('channel', ['telegram', 'feishu'])
def test_native_chat_grant_is_narrow_replay_safe_and_revocable(conn, config, channel):
    bug = binding(conn)
    observe(conn, bug)
    options = execute_control(conn, config, message(config, f"bug grant-options {bug['bug_id']}", channel, 'options'), control_channel=channel)
    assert 'title' in options['text']
    command = issue_command(bug['bug_id'])
    msg = message(config, command, channel, 'grant-message')
    first = execute_control(conn, config, msg, control_channel=channel)
    assert '授权不会立即执行' in first['text']
    grant = conn.execute('SELECT * FROM project_bug_grants').fetchone()
    scope = json.loads(grant['scope_json'])
    assert scope['bug_ids'] == [bug['bug_id']]
    assert scope['actions'] == ['bug.read', 'bug.comment', 'bug.fields']
    assert scope['fields'] == ['title']
    assert scope['transitions'] == scope['repositories'] == scope['devices'] == []
    assert execute_control(conn, config, msg, control_channel=channel) == first
    assert conn.execute('SELECT count(*) FROM project_bug_grants').fetchone()[0] == 1
    listing = execute_control(conn, config, message(config, f"bug grants {bug['bug_id']}", channel, 'list'), control_channel=channel)
    assert grant['grant_id'] in listing['text']
    edited = ControlMessage(msg.user_id, msg.chat_id, msg.message_id,
                            issue_command(bug['bug_id'], fields=[]))
    with pytest.raises(BugConflict, match='intent'):
        execute_control(conn, config, edited, control_channel=channel)
    revoke = message(config, f"bug revoke-grant {grant['grant_id']}", channel, 'revoke')
    result = execute_control(conn, config, revoke, control_channel=channel)
    assert '已撤销' in result['text']
    assert execute_control(conn, config, revoke, control_channel=channel) == result
    assert conn.execute('SELECT count(*) FROM project_bug_grant_events WHERE kind="revoked"').fetchone()[0] == 1
    assert 'revoked' in execute_control(conn, config, msg, control_channel=channel)['text']


def test_chat_grant_rejects_unobserved_fields_and_foreign_identity(conn, config):
    bug = binding(conn)
    observe(conn, bug)
    bad = message(config, issue_command(bug['bug_id'], fields=['not-observed']), 'telegram', 'bad-field')
    with pytest.raises(ValueError, match='已观测'):
        execute_control(conn, config, bad, control_channel='telegram')
    valid = message(config, issue_command(bug['bug_id']), 'telegram', 'foreign')
    foreign = ControlMessage('foreign-user', valid.chat_id, valid.message_id, valid.text)
    with pytest.raises(ApprovalError):
        execute_control(conn, config, foreign, control_channel='telegram')
    assert conn.execute('SELECT count(*) FROM project_bug_grants').fetchone()[0] == 0


def test_chat_grant_rejects_unrequested_authority(conn, config):
    bug = binding(conn)
    observe(conn, bug)
    command = f"bug grant {bug['bug_id']} 8 '{{\"comment\":true,\"fields\":[],\"actions\":[\"bug.close\"]}}'"
    with pytest.raises(ValueError, match='仅支持'):
        execute_control(conn, config, message(config, command, 'telegram'), control_channel='telegram')
    assert conn.execute('SELECT count(*) FROM project_bug_grants').fetchone()[0] == 0


@pytest.mark.parametrize('channel', ['telegram', 'feishu'])
def test_native_gateway_routes_grant_without_model_or_external_writer(conn, config, tmp_path, monkeypatch, channel):
    import asyncio
    from types import SimpleNamespace

    import yaml
    from test_hermes_control_plugin import FakeAdapter, wait_for_receipts, write_runtime

    from k3_support.hermes_plugin import pre_gateway_dispatch

    bug = binding(conn)
    observe(conn, bug)
    msg = message(config, issue_command(bug['bug_id'], comment=False, fields=[]), channel, 'gateway-grant')
    config.path.write_text(yaml.safe_dump(config.raw))
    runtime = write_runtime(tmp_path, config.path)
    monkeypatch.setenv('K3_SUPPORT_CONTROL_PLUGIN_CONFIG', str(runtime))

    async def exercise():
        adapter = FakeAdapter()
        gateway = SimpleNamespace(adapters={channel: adapter})
        event = SimpleNamespace(text=msg.text, message_id=msg.message_id, raw_message=None,
                                source=SimpleNamespace(platform=channel, user_id=msg.user_id,
                                                       chat_id=msg.chat_id))
        assert pre_gateway_dispatch(event, gateway)['action'] == 'skip'
        await wait_for_receipts(adapter)
        assert '授权不会立即执行' in adapter.sent[0][1]

    asyncio.run(exercise())
    scope = json.loads(conn.execute('SELECT scope_json FROM project_bug_grants').fetchone()[0])
    assert scope['actions'] == ['bug.read']
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0] == 0
