"""Shared approval routes over real aggregation of synthetic execution receipts."""
# ruff: noqa: F811
from dataclasses import replace
from datetime import timedelta

import pytest
from test_project_chat_control import message
from test_project_verification_runs import context  # noqa: F401
from test_verification_reviews import execute_fixture, payload

from k3_support import project_close_gate as gate
from k3_support import project_verification_reviews as reviews
from k3_support.approvals import ApprovalError
from k3_support.control import ControlError, execute_control
from k3_support.project_bugs import BugConflict
from k3_support.timeutil import utc_now


def setup(conn, context, channel='telegram'):
    ctx, run = execute_fixture(conn, context)
    cfg = ctx[0]
    message(cfg, 'noop', channel)
    reviews.record(conn, actor=cfg.control_operator_id, payload=payload(conn, run))
    return cfg, ctx[2], run


def request(conn, cfg, bug, key='request-1'):
    return gate.request(conn, actor=cfg.control_operator_id, request_id=key,
                        bug_id=bug['bug_id'], change={'transition_id':'to-close','target_status_id':'closed'},
                        expires_at=(utc_now()+timedelta(hours=1)).isoformat())


def chat(conn, cfg, text, channel='telegram', key='message-1'):
    return execute_control(conn, cfg, message(cfg,text,channel,key), control_channel=channel)


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_preview_decision_replay_and_identity(conn,context,channel):
    cfg,bug,_=setup(conn,context,channel)
    approval=request(conn,cfg,bug)
    aid=approval['approval_id']
    assert aid in chat(conn,cfg,'bug approvals '+bug['bug_id'],channel)['text']
    preview=chat(conn,cfg,'bug approval '+aid,channel)
    assert 'to-close' in preview['text'] and 'closed' in preview['text']
    command=preview['commands'][0].split('\n',1)[1]
    msg=message(cfg,command,channel,'decision')
    with pytest.raises(ApprovalError):
        execute_control(conn,cfg,replace(msg,user_id='foreign'),control_channel=channel)
    result=execute_control(conn,cfg,msg,control_channel=channel)
    before=list(conn.iterdump())
    assert execute_control(conn,cfg,msg,control_channel=channel)==result
    assert list(conn.iterdump())==before
    with pytest.raises(BugConflict,match='native message ID.*different'):
        chat(conn,cfg,command.replace(approval['action_digest'],'wrong'),channel,'decision')
    with pytest.raises(BugConflict,match='native message ID.*different'):
        chat(conn,cfg,command.replace('approve-close','deny-close'),channel,'decision')
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0]==0


def test_changed_evidence_is_deny_only_and_reads_do_not_mutate(conn,context):
    cfg,bug,run=setup(conn,context)
    approval=request(conn,cfg,bug)
    reviews.record(conn,actor=cfg.control_operator_id,payload=payload(conn,run,'failed'))
    before=list(conn.iterdump())
    view=gate.detail(conn,approval['approval_id'])
    assert list(conn.iterdump())==before
    assert not view['can_approve'] and view['can_deny']
    preview=chat(conn,cfg,'bug approval '+approval['approval_id'])
    assert len(preview['commands'])==1 and 'deny-close' in preview['commands'][0]
    command=preview['commands'][0].split('\n',1)[1]
    with pytest.raises(BugConflict,match='evidence changed'):
        chat(conn,cfg,command.replace('deny-close','approve-close'),key='stale-approve')
    chat(conn,cfg,command,key='deny')
    assert gate.detail(conn,approval['approval_id'])['status']=='denied'


def test_message_cannot_decide_another_approval(conn,context):
    cfg,bug,_=setup(conn,context)
    first=request(conn,cfg,bug)
    chat(conn,cfg,f"bug deny-close {first['approval_id']} {first['action_digest']}",key='reused')
    second=request(conn,cfg,bug,'request-2')
    with pytest.raises(BugConflict,match='native message ID.*different'):
        chat(conn,cfg,f"bug deny-close {second['approval_id']} {second['action_digest']}",key='reused')
    assert gate.detail(conn,second['approval_id'])['status']=='requested'


def test_bound_feishu_callback_reaches_same_gate(conn,context):
    cfg,bug,_=setup(conn,context,'feishu')
    approval=request(conn,cfg,bug)
    preview=chat(conn,cfg,'bug approval '+approval['approval_id'],'feishu','preview')
    cid=preview['card_id']
    chat(conn,cfg,f'card-bind {cid} om_delivered','feishu','preview')
    msg=message(cfg,f'card-action {cid} 0','feishu','callback-event')
    with pytest.raises(ControlError,match='not bound'):
        execute_control(conn,cfg,replace(msg,source_card_message_id='om_foreign'),control_channel='feishu')
    result=execute_control(conn,cfg,replace(msg,source_card_message_id='om_delivered'),control_channel='feishu')
    assert '已批准待消费' in result['text']
    assert gate.detail(conn,approval['approval_id'])['status']=='approved'
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0]==0
